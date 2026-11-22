// Appended to RecordingController.swift in a temporary build by test_recording_controller.py.
// No production test hooks and no microphone/TCC calls.

extension RecordingController {
    static func runSyntheticVerification() throws {
        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["FENNEC_RECORDER_TEST_DIR"]!).appendingPathComponent("run")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let endpoint = URL(string: ProcessInfo.processInfo.environment["FENNEC_RECORDER_TEST_ENDPOINT"]!)!
        let suite = "fennec.recorder.test." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        func controller(_ name: String) -> RecordingController {
            RecordingController(recordingsDirectory: root.appendingPathComponent(name), baseURL: endpoint, defaults: defaults)
        }
        func buffer(_ frames: AVAudioFrameCount, amplitude: Float = 0.2) -> AVAudioPCMBuffer {
            let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
            let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
            b.frameLength = frames
            for channel in 0..<2 {
                for i in 0..<Int(frames) { b.floatChannelData![channel][i] = amplitude * sin(Float(i) * 0.03) }
            }
            return b
        }
        func pump(until condition: () -> Bool, timeout: Double = 15) {
            let deadline = Date().addingTimeInterval(timeout)
            while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            precondition(condition(), "Timed out")
        }
        let normal = controller("normal")
        precondition(normal.snapshot()["state"] as? String == "idle")
        normal.handleCommand("configure", options: ["autoTranscribe": true, "silenceSeconds": 0, "maxSegmentSeconds": 60])
        precondition(defaults.bool(forKey: "nativeRecorder.autoTranscribe"))
        normal.writerQueue.sync {
            normal.settings.maxSegmentSeconds = 0.15
            for _ in 0..<10 { normal.write(buffer(2048), receivedAt: Date()) }
            normal.sealSegment(reason: "manual_stop")
        }
        normal.deliveryQueue.sync {}
        pump(until: { normal.pendingImports == 0 && normal.lastSaved != nil })
        let output = try FileManager.default.contentsOfDirectory(at: normal.recordingsDirectory, includingPropertiesForKeys: nil).filter { $0.pathExtension == "m4a" }
        precondition(output.count == 3, "Expected 3 continuous segments, got \(output.count)")
        var totalDuration = 0.0
        for file in output {
            let audio = try AVAudioFile(forReading: file)
            precondition(audio.fileFormat.sampleRate == 16000 && audio.fileFormat.channelCount == 1)
            totalDuration += Double(audio.length) / audio.processingFormat.sampleRate
        }
        precondition(abs(totalDuration - 20480.0 / 48000) < 0.1)
        precondition(normal.lastError.isEmpty, "Recovered delivery error must clear")
        print("PASS continuous split, AAC 16kHz mono, duration preserved, offline import retry, settings persisted")

        let short = controller("short")
        short.writerQueue.sync {
            short.write(buffer(128), receivedAt: Date())
            short.sealSegment(reason: "manual_stop")
        }
        short.deliveryQueue.sync {}
        pump(until: { short.pendingImports == 0 && short.lastSaved != nil })
        print("PASS 2.7ms nonempty recording retained and imported")

        let interrupted = controller("recovery")
        interrupted.writerQueue.sync {
            interrupted.write(buffer(48000), receivedAt: Date())
            // Simulate a process exit after PCM data reaches CAF but before sealing.
            interrupted.audioFile = nil
            interrupted.segment = nil
        }
        let recovered = controller("recovery")
        recovered.deliveryQueue.sync {}
        pump(until: { recovered.pendingImports == 0 && recovered.lastSaved != nil })
        let recoveryFiles = try FileManager.default.contentsOfDirectory(at: recovered.recordingsDirectory, includingPropertiesForKeys: nil)
        precondition(recoveryFiles.contains { $0.pathExtension == "caf" })
        precondition(recoveryFiles.contains { $0.pathExtension == "m4a" })
        print("PASS unsealed CAF crash recovery and raw-file retention")

        let renameFixture = controller("rename-crash")
        let renameStem = "rec_2026-11-22_19-00-00_" + UUID().uuidString.lowercased()
        let renameManifest = Manifest(captureID: UUID().uuidString.lowercased(), stem: renameStem,
                                      startedAt: Date(), autoTranscribe: false, phase: "capturing")
        try FileManager.default.copyItem(at: output[0], to: renameFixture.url(renameStem, "m4a"))
        try renameFixture.saveManifest(renameManifest)
        let renameRecovered = controller("rename-crash")
        renameRecovered.deliveryQueue.sync {}
        pump(until: { renameRecovered.pendingImports == 0 && renameRecovered.lastSaved != nil })
        precondition(renameRecovered.lastError.isEmpty)
        print("PASS final rename-before-manifest crash fills missing endedAt from AAC duration")

        let crashDirectory = root.appendingPathComponent("true-crash")
        let crash = Process()
        crash.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0])
        crash.arguments = ["--crash-fixture", crashDirectory.path, endpoint.absoluteString]
        try crash.run()
        crash.waitUntilExit()
        precondition(crash.terminationStatus == 0)
        let trueRecovered = controller("true-crash")
        trueRecovered.deliveryQueue.sync {}
        pump(until: { trueRecovered.pendingImports == 0 && trueRecovered.lastSaved != nil })
        print("PASS actual process _exit with open CAF recovers without AVAudioFile deinit")

        // Turning auto transcription off must affect the segment already being captured.
        let toggled = controller("toggle-auto")
        toggled.handleCommand("configure", options: ["autoTranscribe": true])
        toggled.writerQueue.sync { toggled.write(buffer(4800), receivedAt: Date()) }
        toggled.handleCommand("configure", options: ["autoTranscribe": false])
        toggled.writerQueue.sync { toggled.sealSegment(reason: "manual_stop") }
        toggled.deliveryQueue.sync {}
        pump(until: { toggled.pendingImports == 0 && toggled.lastSaved != nil })
        let toggleJSON = try FileManager.default.contentsOfDirectory(at: toggled.recordingsDirectory, includingPropertiesForKeys: nil).first { $0.pathExtension == "json" }!
        let toggleManifest = try JSONSerialization.jsonObject(with: Data(contentsOf: toggleJSON)) as! [String: Any]
        precondition(toggleManifest["autoTranscribe"] as? Bool == false)
        precondition(toggleManifest["phase"] as? String == "delivered")
        print("PASS disabling auto transcription applies to the current segment")

        let rejected = controller("permanent-error")
        rejected.writerQueue.sync {
            rejected.write(buffer(4800), receivedAt: Date())
            rejected.segment!.captureID = "reject_" + UUID().uuidString.lowercased()
            rejected.sealSegment(reason: "manual_split")
            rejected.write(buffer(4800), receivedAt: Date())
            rejected.sealSegment(reason: "manual_stop")
        }
        rejected.deliveryQueue.sync {}
        pump(until: {
            rejected.deliveryQueue.sync {
                rejected.pending.count == 1 && rejected.pending.values.first?.phase == "recovery_required" && !rejected.importing
            }
        })
        let rejectedJSONs = try FileManager.default.contentsOfDirectory(at: rejected.recordingsDirectory, includingPropertiesForKeys: nil).filter { $0.pathExtension == "json" }
        let rejectedPhases = try rejectedJSONs.map { try JSONSerialization.jsonObject(with: Data(contentsOf: $0)) as! [String: Any] }.compactMap { $0["phase"] as? String }
        precondition(rejectedPhases.contains("delivered") && rejectedPhases.contains("recovery_required"))
        print("PASS permanent HTTP 422 preserves failed recording and allows later imports")

        // Simulate deleting the imported session; retained raw audio must not resurrect it.
        let deliveredRawFiles = try FileManager.default.contentsOfDirectory(at: trueRecovered.recordingsDirectory, includingPropertiesForKeys: nil)
        for file in deliveredRawFiles where file.pathExtension == "m4a" { try FileManager.default.removeItem(at: file) }
        precondition(deliveredRawFiles.contains { $0.pathExtension == "caf" })
        let tombstoneRecovered = controller("true-crash")
        tombstoneRecovered.deliveryQueue.sync {}
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        precondition(tombstoneRecovered.pendingImports == 0 && tombstoneRecovered.lastSaved == nil)
        let afterDeletion = try FileManager.default.contentsOfDirectory(at: tombstoneRecovered.recordingsDirectory, includingPropertiesForKeys: nil)
        precondition(!afterDeletion.contains { $0.pathExtension == "m4a" })
        print("PASS delivered tombstone prevents retained CAF from resurrecting a deleted session")

        defaults.removeObject(forKey: "nativeRecorder.silenceSeconds")
        func listeningController(_ name: String) -> RecordingController {
            let item = controller(name)
            item.captureMode = "listening"
            item.state = "listening" // Synthetic capture only; never starts an engine.
            item.userWantsRecording = true
            // Synthetic streams have no hardware watchdog timestamps. Disable the
            // periodic retry path so a test can never attempt a real engine restart.
            item.timer?.invalidate()
            item.timer = nil
            item.testSpeechStatus = NativeSpeechDetector.Status(confidence: 0, state: "ready")
            return item
        }
        let audioEpoch = Date()
        func feed(_ item: RecordingController, amplitude: Float, count: Int, confidence: Double = 0) {
            let pcm = buffer(4800, amplitude: amplitude)
            item.writerQueue.sync {
                item.testSpeechStatus = NativeSpeechDetector.Status(confidence: confidence, state: "ready")
                for _ in 0..<count {
                    item.write(pcm, receivedAt: audioEpoch.addingTimeInterval(item.capturedSeconds))
                }
            }
        }
        let waiting = listeningController("listening-wait")
        precondition(waiting.settings.silenceSeconds == 15)
        feed(waiting, amplitude: 0.002, count: 700)
        precondition(waiting.audioFile == nil && waiting.segment == nil)
        let waitingSnapshot = waiting.snapshot()
        precondition(waitingSnapshot["state"] as? String == "listening")
        precondition(waitingSnapshot["listening"] as? Bool == true)
        precondition(waitingSnapshot["recording"] as? Bool == false)
        precondition(waitingSnapshot["activeStartedAt"] is NSNull)
        precondition(waiting.waveformRecent.count <= 120 && waiting.preRollSeconds < 2.11)
        precondition(abs(waiting.capturedSeconds - 70) < 0.01)
        precondition(waiting.noiseFloor > 0.0005 && waiting.noiseFloor < 0.003)
        precondition(abs(waiting.signalToNoise - waiting.measuredRMS / waiting.noiseFloor) < 0.0001)
        precondition((try? FileManager.default.contentsOfDirectory(atPath: waiting.recordingsDirectory.path).isEmpty) == true)
        waiting.writerQueue.sync {
            let pcm = buffer(4800, amplitude: 0.002)
            waiting.noiseFloor = Self.rms(pcm) / 2
            waiting.testSpeechStatus = NativeSpeechDetector.Status(confidence: 0.95, state: "ready")
            waiting.write(pcm, receivedAt: audioEpoch.addingTimeInterval(waiting.capturedSeconds))
        }
        precondition(abs(waiting.signalToNoise - 2) < 0.0001, "SNR must be a linear multiplier, not decibels")
        waiting.stop()
        pump(until: { waiting.state == "idle" })
        waiting.deliveryQueue.sync {}
        print("PASS quiet listening creates no files; adaptive floor, linear SNR, bounded 60-second waveform/pre-roll")

        let tinyCallbacks = listeningController("tiny-callback-preroll")
        tinyCallbacks.writerQueue.sync {
            let tiny = buffer(64, amplitude: 0)
            for _ in 0..<2250 {
                tinyCallbacks.write(tiny, receivedAt: audioEpoch.addingTimeInterval(tinyCallbacks.capturedSeconds))
            }
        }
        precondition(tinyCallbacks.preRollSeconds >= 2 && tinyCallbacks.preRollSeconds < 2.11)
        precondition(tinyCallbacks.preRoll.count <= 22, "Tiny callbacks must coalesce without reducing pre-roll duration")
        tinyCallbacks.stop()
        pump(until: { tinyCallbacks.state == "idle" })
        print("PASS 64-frame callbacks retain two seconds of pre-roll with bounded object count")

        let listener = listeningController("listening-speech")
        listener.handleCommand("configure", options: ["silenceSeconds": 5])
        feed(listener, amplitude: 0, count: 20)
        let wordStarted = audioEpoch.addingTimeInterval(listener.capturedSeconds)
        feed(listener, amplitude: 0.2, count: 3, confidence: 0)
        precondition(listener.audioFile == nil)
        // A short word is already over when its model result arrives.
        feed(listener, amplitude: 0, count: 2, confidence: 0.95)
        precondition(listener.audioFile != nil)
        precondition(listener.activeSegmentStartedAt! < wordStarted)
        precondition(listener.segmentSeconds >= 1.9)
        feed(listener, amplitude: 0, count: 60)
        precondition(listener.audioFile == nil && listener.snapshot()["state"] as? String == "listening")
        precondition(listener.snapshot()["activeStartedAt"] is NSNull)
        listener.deliveryQueue.sync {}
        pump(until: { listener.pendingImports == 0 && listener.lastSaved != nil })
        print("PASS delayed short-word detection saves pre-roll; silence ends the file but keeps listening")

        listener.record()
        listener.writerQueue.sync {}
        feed(listener, amplitude: 0, count: 70)
        precondition(listener.audioFile != nil && listener.forcedRecording)
        listener.endSegment()
        listener.writerQueue.sync {}
        precondition(listener.audioFile == nil && !listener.forcedRecording)
        feed(listener, amplitude: 0.2, count: 5, confidence: 0.95)
        precondition(listener.audioFile == nil, "Manual end cooldown must suppress immediate reopening")
        feed(listener, amplitude: 0.2, count: 20, confidence: 0.95)
        precondition(listener.audioFile != nil && !listener.forcedRecording)
        listener.split()
        listener.writerQueue.sync {}
        feed(listener, amplitude: 0.2, count: 1, confidence: 0.95)
        precondition(abs(listener.segmentSeconds - 0.1) < 0.001, "Split must not duplicate pre-roll")
        listener.writerQueue.sync { listener.settings.maxSegmentSeconds = 0.3 }
        feed(listener, amplitude: 0.2, count: 5, confidence: 0.95)
        precondition(listener.audioFile == nil || listener.segmentSeconds <= 0.31)
        listener.stop()
        pump(until: { listener.state == "idle" })
        listener.deliveryQueue.sync {}
        print("PASS forced recording ignores silence, endSegment keeps listening with cooldown, split/max duration stay continuous")

        let volume = listeningController("volume-only")
        volume.handleCommand("configure", options: ["speechDetection": false, "silenceSeconds": 5, "sensitivity": 3])
        feed(volume, amplitude: 0.001, count: 15)
        feed(volume, amplitude: 0.1, count: 5)
        precondition(volume.audioFile != nil)
        precondition(volume.speechStatus == "disabled" && volume.speechConfidence == 0)
        volume.stop()
        pump(until: { volume.state == "idle" })
        volume.deliveryQueue.sync {}
        print("PASS disabling human-speech detection explicitly uses volume gating")

        let degraded = listeningController("degraded-volume")
        degraded.settings.speechDetection = true
        degraded.writerQueue.sync {
            degraded.testSpeechStatus = NativeSpeechDetector.Status(state: "unavailable", error: "Fixture classifier unavailable; volume fallback")
            for _ in 0..<15 { degraded.write(buffer(4800, amplitude: 0), receivedAt: audioEpoch.addingTimeInterval(degraded.capturedSeconds)) }
            for _ in 0..<5 { degraded.write(buffer(4800), receivedAt: audioEpoch.addingTimeInterval(degraded.capturedSeconds)) }
        }
        precondition(degraded.audioFile != nil && degraded.speechStatus == "unavailable" && !degraded.speechError.isEmpty)
        degraded.stop()
        pump(until: { degraded.state == "idle" })
        degraded.deliveryQueue.sync {}
        print("PASS unavailable classifier clearly reports degraded volume detection")

        // Actual built-in SoundAnalysis classification, fed at real-time pace. No mic.
        let speechFixture = root.appendingPathComponent("synthetic-speech.aiff")
        let say = Process()
        say.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        say.arguments = ["-v", "Samantha", "-o", speechFixture.path,
                         "This is a local speech detection test. Please keep listening while I speak. We can save a short recording without opening any microphone."]
        try say.run()
        say.waitUntilExit()
        precondition(say.terminationStatus == 0)
        let fixture = try AVAudioFile(forReading: speechFixture)
        let actualListener = listeningController("actual-soundanalysis")
        actualListener.testSpeechStatus = nil
        actualListener.settings.speechDetection = true
        actualListener.settings.silenceSeconds = 5
        let chunkFrames = AVAudioFrameCount(fixture.processingFormat.sampleRate * 0.05)
        var position: AVAudioFramePosition = 0
        var peakConfidence = 0.0
        var readyCount = 0
        var firstOpenStatus: String?
        while fixture.framePosition < fixture.length {
            let pcm = AVAudioPCMBuffer(pcmFormat: fixture.processingFormat, frameCapacity: chunkFrames)!
            try fixture.read(into: pcm)
            actualListener.writerQueue.sync {
                actualListener.write(pcm, receivedAt: audioEpoch.addingTimeInterval(actualListener.capturedSeconds))
                if actualListener.audioFile != nil && firstOpenStatus == nil { firstOpenStatus = actualListener.speechStatus }
            }
            position += AVAudioFramePosition(pcm.frameLength)
            Thread.sleep(forTimeInterval: Double(pcm.frameLength) / fixture.processingFormat.sampleRate)
            let result = actualListener.speechDetector!.snapshot(at: Double(position) / fixture.processingFormat.sampleRate)
            if result.state == "ready" { readyCount += 1; peakConfidence = max(peakConfidence, result.confidence) }
        }
        precondition(firstOpenStatus == "ready", "Actual speech must open a segment through the classifier, not volume fallback")
        let silence = AVAudioPCMBuffer(pcmFormat: fixture.processingFormat, frameCapacity: chunkFrames)!
        silence.frameLength = chunkFrames
        for audioBuffer in UnsafeMutableAudioBufferListPointer(silence.mutableAudioBufferList) {
            if let data = audioBuffer.mData { memset(data, 0, Int(audioBuffer.mDataByteSize)) }
        }
        for _ in 0..<140 {
            actualListener.writerQueue.sync {
                actualListener.write(silence, receivedAt: audioEpoch.addingTimeInterval(actualListener.capturedSeconds))
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        precondition(actualListener.audioFile == nil && actualListener.snapshot()["state"] as? String == "listening")
        actualListener.stop()
        pump(until: { actualListener.state == "idle" })
        actualListener.deliveryQueue.sync {}
        pump(until: { actualListener.pendingImports == 0 && actualListener.lastSaved != nil })
        precondition(readyCount > 0 && peakConfidence >= 0.45, "Actual SoundAnalysis did not classify synthesized speech: confidence \(peakConfidence), ready \(readyCount)")
        print("PASS actual on-device SoundAnalysis speech classifier: peak confidence \(String(format: "%.3f", peakConfidence)), \(readyCount) ready observations")

        let overflow = controller("overflow")
        overflow.acceptingAudio = true
        overflow.captureGeneration = 7
        overflow.writerQueue.suspend()
        for _ in 0..<65 { overflow.receive(buffer(2048), generation: 7) }
        precondition(!overflow.acceptingAudio)
        overflow.writerQueue.resume()
        pump(until: { overflow.state == "error" })
        overflow.deliveryQueue.sync {}
        pump(until: { overflow.pendingImports == 0 && overflow.lastSaved != nil })
        precondition(overflow.capturedSeconds > 2.7 && overflow.capturedSeconds < 2.74)
        print("PASS bounded backpressure explicitly stops and preserves 64 queued buffers")
        let beforeLate = overflow.segmentCount
        overflow.receive(buffer(2048), generation: 7)
        overflow.writerQueue.sync {}
        pump(until: { overflow.state == "error" }, timeout: 1)
        precondition(overflow.segmentCount == beforeLate)
        print("PASS stopped capture rejects late callbacks without opening another file")
        var terminated = false
        normal.prepareForTermination { terminated = true }
        pump(until: { terminated })
        precondition(normal.state == "idle")
        print("PASS asynchronous termination seal completion")
        print("PASS all fixtures isolated in temporary storage")
    }
}
extension RecordingController {
    static func writeCrashFixture(_ directory: String, _ endpoint: String) throws {
        let recorder = RecordingController(recordingsDirectory: URL(fileURLWithPath: directory), baseURL: URL(string: endpoint)!)
        let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48000)!
        b.frameLength = 48000
        for i in 0..<48000 { b.floatChannelData![0][i] = 0.2 * sin(Float(i) * 0.02) }
        recorder.writerQueue.sync { recorder.write(b, receivedAt: Date()) }
        _exit(0)
    }
}
if CommandLine.arguments.contains("--crash-fixture") {
    try RecordingController.writeCrashFixture(CommandLine.arguments[2], CommandLine.arguments[3])
} else {
    try RecordingController.runSyntheticVerification()
}
