// prowl-listen: on-device speech-to-text helper for Prowl.
//
// Usage: prowl-listen [maxSeconds] [locale] [outputFile]
//        (defaults: 12, "en-US", stdout)
//
// When outputFile is given the transcript is written there as well as to
// stdout. That exists so the app can be started through LaunchServices
// (`open -a ProwlListen.app`), which is the only way macOS treats it as its own
// TCC identity — launched as a plain child process, the microphone permission
// belongs to whichever process happened to be the parent (Terminal, Python,
// a menu-bar app), so it works from one and is killed from another.
//
// Captures the default input device, runs offline SFSpeechRecognizer with
// requiresOnDeviceRecognition, streams partial results, and prints only the
// final best transcript to stdout. All status/logging goes to stderr.
// Stops on the first of: final result, ~1.5s of silence after speech begins,
// or the maxSeconds hard cap. Exits 0 on success, non-zero on any error.

import Foundation
import AVFoundation
import Speech

// MARK: - stderr logging

func err(_ msg: String) {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8) ?? Data())
}

// MARK: - arguments

let args = CommandLine.arguments

// Streaming mode: stay resident and append one line per recognised phrase to
// the output file, instead of capturing a single fixed-length window. Wake-word
// listening needs this — a window-based listener is deaf between windows, and
// only reports once the window closes, so "hey Bob" is either missed outright
// or answered many seconds late.
let streaming = args.contains("--stream")
let positional = args.dropFirst().filter { !$0.hasPrefix("--") }

let maxSeconds: Double = {
    guard let first = positional.first, let v = Double(first), v > 0 else { return 12.0 }
    return v
}()
let localeID: String = {
    let idx = positional.index(positional.startIndex, offsetBy: 1, limitedBy: positional.endIndex)
    guard let i = idx, i < positional.endIndex, !positional[i].isEmpty else { return "en-US" }
    return positional[i]
}()
// Optional file to write results to (see the note at the top).
let outputPath: String? = {
    let idx = positional.index(positional.startIndex, offsetBy: 2, limitedBy: positional.endIndex)
    guard let i = idx, i < positional.endIndex, !positional[i].isEmpty else { return nil }
    return positional[i]
}()

// Write `text` to outputPath, if one was given. Failures are ignored: stdout
// is still authoritative when the caller can read it.
func writeOutput(_ text: String, code: Int32) {
    guard let path = outputPath else { return }
    // The exit code goes on the first line so a caller reading only the file
    // can still tell success from failure.
    let payload = "\(code)\n" + text + "\n"
    try? payload.write(toFile: path, atomically: true, encoding: .utf8)
}

// Time of silence (after speech began) that triggers a stop.
let silenceTimeout: Double = 1.5

// MARK: - authorization

// Request Speech recognition authorization (blocking via semaphore).
func requestSpeechAuth() -> SFSpeechRecognizerAuthorizationStatus {
    let sem = DispatchSemaphore(value: 0)
    var status: SFSpeechRecognizerAuthorizationStatus = .notDetermined
    SFSpeechRecognizer.requestAuthorization { s in
        status = s
        sem.signal()
    }
    sem.wait()
    return status
}

// Request microphone access (blocking via semaphore).
func requestMicAuth() -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    if #available(macOS 14.0, *) {
        AVAudioApplication.requestRecordPermission { ok in
            granted = ok
            sem.signal()
        }
    } else {
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            granted = ok
            sem.signal()
        }
    }
    sem.wait()
    return granted
}

let speechStatus = requestSpeechAuth()
guard speechStatus == .authorized else {
    switch speechStatus {
    case .denied:
        err("prowl-listen: speech recognition denied. Grant it in "
            + "System Settings > Privacy & Security > Speech Recognition.")
    case .restricted:
        err("prowl-listen: speech recognition is restricted on this device.")
    default:
        err("prowl-listen: speech recognition not authorized.")
    }
    writeOutput("", code: 2)
    exit(2)
}

guard requestMicAuth() else {
    err("prowl-listen: microphone access denied. Grant it in "
        + "System Settings > Privacy & Security > Microphone.")
    writeOutput("", code: 3)
    exit(3)
}

// MARK: - recognizer

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)) else {
    err("prowl-listen: no recognizer available for locale '\(localeID)'.")
    writeOutput("", code: 4)
    exit(4)
}
guard recognizer.isAvailable else {
    err("prowl-listen: recognizer for '\(localeID)' is not available right now.")
    writeOutput("", code: 4)
    exit(4)
}

// MARK: - shared state

let engine = AVAudioEngine()
let stateQueue = DispatchQueue(label: "prowl.listen.state")
let done = DispatchSemaphore(value: 0)

var bestTranscript = ""
var finished = false
var exitCode: Int32 = 0
var recognitionTask: SFSpeechRecognitionTask?
var silenceTimer: DispatchSourceTimer?
var restartTimer: DispatchSourceTimer?

// The audio tap runs on a realtime thread and the state queue swaps the
// request out from under it on every restart, so the pointer needs a lock.
var currentRequest: SFSpeechAudioBufferRecognitionRequest?
let reqLock = NSLock()

func setRequest(_ r: SFSpeechAudioBufferRecognitionRequest?) {
    reqLock.lock()
    currentRequest = r
    reqLock.unlock()
}

func activeRequest() -> SFSpeechAudioBufferRecognitionRequest? {
    reqLock.lock()
    defer { reqLock.unlock() }
    return currentRequest
}

func makeRequest() -> SFSpeechAudioBufferRecognitionRequest {
    let r = SFSpeechAudioBufferRecognitionRequest()
    r.shouldReportPartialResults = true
    if recognizer.supportsOnDeviceRecognition {
        r.requiresOnDeviceRecognition = true
    }
    return r
}

// Append one finished utterance to the output file. Streaming mode only: the
// caller tails this file, so each line must be a complete thought.
func appendLine(_ text: String) {
    guard let path = outputPath, !text.isEmpty else { return }
    let line = text + "\n"
    guard let data = line.data(using: .utf8) else { return }
    if let handle = FileHandle(forWritingAtPath: path) {
        handle.seekToEndOfFile()
        handle.write(data)
        handle.closeFile()
    } else {
        try? line.write(toFile: path, atomically: true, encoding: .utf8)
    }
}

// Tear down the audio engine and recognition request exactly once, then wake
// the main thread. Safe to call from any thread; guarded by `finished`.
func finish(code: Int32) {
    stateQueue.async {
        if finished { return }
        finished = true
        exitCode = code

        silenceTimer?.cancel()
        silenceTimer = nil
        restartTimer?.cancel()
        restartTimer = nil

        if engine.isRunning {
            engine.stop()
        }
        engine.inputNode.removeTap(onBus: 0)
        activeRequest()?.endAudio()
        setRequest(nil)
        recognitionTask?.cancel()
        recognitionTask = nil

        done.signal()
    }
}

// MARK: - recognition

// Start (or restart) a recognition task. In streaming mode this is called once
// per utterance: a finished phrase is written out and a fresh request begins,
// so the microphone never closes and there is no deaf gap between windows.
func startRecognition() {
    let req = makeRequest()
    setRequest(req)
    recognitionTask?.cancel()
    recognitionTask = recognizer.recognitionTask(with: req) { result, error in
        stateQueue.async {
            if finished { return }

            if let result = result {
                let text = result.bestTranscription.formattedString
                if !text.isEmpty {
                    bestTranscript = text
                    armSilenceTimer()
                }
                if result.isFinal {
                    if streaming {
                        emitAndRestart()
                    } else {
                        err("prowl-listen: final result received.")
                        finish(code: 0)
                    }
                    return
                }
            }

            if let error = error {
                if streaming {
                    // A timed-out or cancelled request is routine here; keep
                    // the microphone open and start the next one.
                    emitAndRestart()
                    return
                }
                // A cancel we initiated surfaces here; ignore once finishing.
                if bestTranscript.isEmpty {
                    err("prowl-listen: recognition error: \(error.localizedDescription)")
                    finish(code: 5)
                } else {
                    finish(code: 0)
                }
            }
        }
    }

    if streaming {
        // Apple's recognizer stops accepting audio after about a minute, so
        // cycle it well before that rather than waiting to be cut off.
        restartTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: stateQueue)
        timer.schedule(deadline: .now() + 45)
        timer.setEventHandler { emitAndRestart() }
        timer.resume()
        restartTimer = timer
    }
}

// Write whatever has been heard so far, then begin a new request. Must be
// called on the state queue.
func emitAndRestart() {
    if finished { return }
    let text = bestTranscript.trimmingCharacters(in: .whitespacesAndNewlines)
    if !text.isEmpty {
        appendLine(text)
        err("prowl-listen: segment: \(text)")
    }
    bestTranscript = ""
    silenceTimer?.cancel()
    silenceTimer = nil
    activeRequest()?.endAudio()
    startRecognition()
}

// (Re)arm the post-speech silence timer. Called on the state queue whenever a
// partial result arrives; if no further audio updates it in `silenceTimeout`
// seconds, we treat speech as complete.
func armSilenceTimer() {
    silenceTimer?.cancel()
    let timer = DispatchSource.makeTimerSource(queue: stateQueue)
    timer.schedule(deadline: .now() + silenceTimeout)
    timer.setEventHandler {
        if bestTranscript.isEmpty { return }
        if streaming {
            emitAndRestart()
        } else {
            err("prowl-listen: stopping after silence.")
            finish(code: 0)
        }
    }
    timer.resume()
    silenceTimer = timer
}

// MARK: - audio engine

let inputNode = engine.inputNode
let format = inputNode.outputFormat(forBus: 0)
guard format.channelCount > 0 else {
    err("prowl-listen: no audio input available.")
    writeOutput("", code: 6)
    exit(6)
}

inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
    activeRequest()?.append(buffer)
}

engine.prepare()
do {
    try engine.start()
} catch {
    err("prowl-listen: could not start audio engine: \(error.localizedDescription)")
    writeOutput("", code: 6)
    exit(6)
}

startRecognition()

if streaming {
    err("prowl-listen: streaming (locale \(localeID)); one line per phrase.")
} else {
    err("prowl-listen: listening for up to \(Int(maxSeconds))s (locale \(localeID))...")
    // Hard cap: stop after maxSeconds no matter what.
    let capTimer = DispatchSource.makeTimerSource(queue: stateQueue)
    capTimer.schedule(deadline: .now() + maxSeconds)
    capTimer.setEventHandler {
        err("prowl-listen: reached max duration.")
        finish(code: 0)
    }
    capTimer.resume()
}

// MARK: - wait for completion

// Block the main thread until recognition finishes; keep the run loop serviced
// so the async callbacks and timers can fire. Streaming mode never finishes on
// its own — it runs until the parent kills it.
while done.wait(timeout: .now() + 0.05) == .timedOut {
    RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
}

let (finalText, finalCode): (String, Int32) = stateQueue.sync {
    (bestTranscript.trimmingCharacters(in: .whitespacesAndNewlines), exitCode)
}

if streaming {
    exit(finalCode)
}

writeOutput(finalCode == 0 ? finalText : "", code: finalCode)
if finalCode == 0 {
    // Only the transcript goes to stdout.
    print(finalText)
    exit(0)
} else {
    exit(finalCode)
}
