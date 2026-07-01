// prowl-listen: on-device speech-to-text helper for Prowl.
//
// Usage: prowl-listen [maxSeconds] [locale]   (defaults: 12, "en-US")
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
let maxSeconds: Double = {
    guard args.count > 1, let v = Double(args[1]), v > 0 else { return 12.0 }
    return v
}()
let localeID: String = (args.count > 2 && !args[2].isEmpty) ? args[2] : "en-US"

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
    exit(2)
}

guard requestMicAuth() else {
    err("prowl-listen: microphone access denied. Grant it in "
        + "System Settings > Privacy & Security > Microphone.")
    exit(3)
}

// MARK: - recognizer

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)) else {
    err("prowl-listen: no recognizer available for locale '\(localeID)'.")
    exit(4)
}
guard recognizer.isAvailable else {
    err("prowl-listen: recognizer for '\(localeID)' is not available right now.")
    exit(4)
}

let request = SFSpeechAudioBufferRecognitionRequest()
request.shouldReportPartialResults = true
if recognizer.supportsOnDeviceRecognition {
    request.requiresOnDeviceRecognition = true
} else {
    err("prowl-listen: on-device recognition unsupported for '\(localeID)'; "
        + "falling back to server recognition.")
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

// Tear down the audio engine and recognition request exactly once, then wake
// the main thread. Safe to call from any thread; guarded by `finished`.
func finish(code: Int32) {
    stateQueue.async {
        if finished { return }
        finished = true
        exitCode = code

        silenceTimer?.cancel()
        silenceTimer = nil

        if engine.isRunning {
            engine.stop()
        }
        engine.inputNode.removeTap(onBus: 0)
        request.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil

        done.signal()
    }
}

// (Re)arm the post-speech silence timer. Called on the state queue whenever a
// partial result arrives; if no further audio updates it in `silenceTimeout`
// seconds, we treat speech as complete and emit what we have.
func armSilenceTimer() {
    silenceTimer?.cancel()
    let timer = DispatchSource.makeTimerSource(queue: stateQueue)
    timer.schedule(deadline: .now() + silenceTimeout)
    timer.setEventHandler {
        if bestTranscript.isEmpty { return }
        err("prowl-listen: stopping after silence.")
        finish(code: 0)
    }
    timer.resume()
    silenceTimer = timer
}

// MARK: - recognition task

recognitionTask = recognizer.recognitionTask(with: request) { result, error in
    stateQueue.async {
        if finished { return }

        if let result = result {
            let text = result.bestTranscription.formattedString
            if !text.isEmpty {
                bestTranscript = text
                armSilenceTimer()
            }
            if result.isFinal {
                err("prowl-listen: final result received.")
                finish(code: 0)
                return
            }
        }

        if let error = error {
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

// MARK: - audio engine

let inputNode = engine.inputNode
let format = inputNode.outputFormat(forBus: 0)
guard format.channelCount > 0 else {
    err("prowl-listen: no audio input available.")
    exit(6)
}

inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
    request.append(buffer)
}

engine.prepare()
do {
    try engine.start()
} catch {
    err("prowl-listen: could not start audio engine: \(error.localizedDescription)")
    exit(6)
}
err("prowl-listen: listening for up to \(Int(maxSeconds))s (locale \(localeID))...")

// Hard cap: stop after maxSeconds no matter what.
let capTimer = DispatchSource.makeTimerSource(queue: stateQueue)
capTimer.schedule(deadline: .now() + maxSeconds)
capTimer.setEventHandler {
    err("prowl-listen: reached max duration.")
    finish(code: 0)
}
capTimer.resume()

// MARK: - wait for completion

// Block the main thread until recognition finishes; keep the run loop serviced
// so the async callbacks and timers can fire.
while done.wait(timeout: .now() + 0.05) == .timedOut {
    RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
}
capTimer.cancel()

let (finalText, finalCode): (String, Int32) = stateQueue.sync {
    (bestTranscript.trimmingCharacters(in: .whitespacesAndNewlines), exitCode)
}

if finalCode == 0 {
    // Only the transcript goes to stdout.
    print(finalText)
    exit(0)
} else {
    exit(finalCode)
}
