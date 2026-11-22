import AppKit
import AVFoundation
import CoreAudio

/// Main-thread API. Audio writing and durable delivery never run on the main thread.
/// This controller does not touch the microphone until the user explicitly starts it.
final class RecordingController {
    var onStatus: (([String: Any]) -> Void)?

    private let recordingsDirectory: URL
    private let baseURL: URL
    private let preferences: UserDefaults
    private let writerQueue = DispatchQueue(label: "fennec.recorder.writer", qos: .userInitiated)
    private let deliveryQueue = DispatchQueue(label: "fennec.recorder.delivery", qos: .utility)
    private let bufferSlots = DispatchSemaphore(value: 64)
    private let metricsLock = NSLock()
    private var acceptingAudio = false
    private var captureGeneration = 0
    private var capturedSeconds: Double = 0
    private var audioLevel: Double = 0
    private var lastBufferTime: TimeInterval = 0
    private var settings = Settings()
    // Audio-derived snapshot values are protected by metricsLock.
    private var captureMode = "manual"
    private var activeSegmentSeconds: Double = 0
    private var activeSegmentStartedAt: Date?
    private var forcedRecording = false
    private var measuredRMS: Double = 0
    private var noiseFloor: Double = 0.0015
    private var signalToNoise: Double = 0
    private var speechConfidence: Double = 0
    private var speechStatus = "disabled"
    private var speechError = ""
    private var waveformRecent: [[String: Double]] = []

    private var engine: AVAudioEngine?
    private var tapInstalled = false
    private var state = "idle"
    private var lastError = ""
    private var lastDeliveryError: String?
    private var lastSaved: String?
    private var inputDevice = "默认麦克风"
    private var pendingImports = 0
    private var segmentCount = 0
    private var userWantsRecording = false
    private var manuallyPaused = false
    private var sleeping = false
    private var terminating = false
    private var operation = 0
    private var resumeAfterSeal = false
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var nextRestartAt = Date.distantPast
    private var captureStartedAt = Date.distantPast

    // writerQueue only
    private var audioFile: AVAudioFile?
    private var segment: Manifest?
    private var segmentSeconds: Double = 0
    private var quietSeconds: Double = 0
    private struct PreRollChunk { let buffer: AVAudioPCMBuffer; let at: Date; let seconds: Double; let rms: Double }
    private var preRoll: [PreRollChunk] = []
    private var preRollSeconds: Double = 0
    private var forceNextSegment = false
    private var continueSegment = false
    private var cooldownUntil: Double = 0
    private var activationSeconds: Double = 0
    private var speechDetector: NativeSpeechDetector?
    private var analysisFramePosition: AVAudioFramePosition = 0
    private var lastWaveformTime = Date.distantPast
    private var noiseSamples: [Double] = []

    // deliveryQueue only. A manifest remains on disk until successful import.
    private var pending: [String: Manifest] = [:]
    private var importing = false
    private var retryScheduled = false
    private var retryDelay: Double = 2

    private struct Settings {
        var autoTranscribe = false
        var silenceSeconds: Double = 15
        var maxSegmentSeconds: Double = 600
        var sensitivity: Double = 3
        var speechDetection = true
    }

    private struct Manifest: Codable {
        var captureID: String
        var stem: String
        var startedAt: Date
        var endedAt: Date?
        var interruptionReason: String?
        var autoTranscribe: Bool
        var phase: String
        var error: String?
    }

    private struct RecorderError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    init(recordingsDirectory: URL, baseURL: URL, defaults: UserDefaults = .standard) {
        self.recordingsDirectory = recordingsDirectory.standardizedFileURL
        self.baseURL = baseURL
        self.preferences = defaults
        settings.autoTranscribe = defaults.bool(forKey: "nativeRecorder.autoTranscribe")
        if let value = defaults.object(forKey: "nativeRecorder.silenceSeconds") as? NSNumber, value.doubleValue.isFinite {
            settings.silenceSeconds = value.doubleValue == 0 ? 0 : min(120, max(5, value.doubleValue))
        }
        if let value = defaults.object(forKey: "nativeRecorder.maxSegmentSeconds") as? NSNumber, value.doubleValue.isFinite {
            settings.maxSegmentSeconds = min(600, max(30, value.doubleValue))
        }
        if let value = defaults.object(forKey: "nativeRecorder.sensitivity") as? NSNumber, value.doubleValue.isFinite {
            settings.sensitivity = min(8, max(1.5, value.doubleValue))
        }
        if defaults.object(forKey: "nativeRecorder.speechDetection") != nil {
            settings.speechDetection = defaults.bool(forKey: "nativeRecorder.speechDetection")
        }
        do {
            try FileManager.default.createDirectory(at: recordingsDirectory, withIntermediateDirectories: true)
        } catch {
            state = "error"
            lastError = "无法创建录音目录：\(error.localizedDescription)"
        }
        observers.append(NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.willSleepNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.willSleep() })
        observers.append(NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.didWake() })
        observers.append(NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main
        ) { [weak self] notification in
            guard let self = self, let changed = notification.object as? AVAudioEngine,
                  changed === self.engine, self.state == "recording" || self.state == "listening" else { return }
            self.interrupt(reason: "input_device_changed", message: "输入设备发生变化，正在重新连接默认麦克风。")
        })
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
        // Snapshot before a user can start recording, so recovery cannot pick up a new live segment.
        let recoveryFiles = (try? FileManager.default.contentsOfDirectory(at: recordingsDirectory,
                                                                         includingPropertiesForKeys: [.creationDateKey])) ?? []
        deliveryQueue.async { [weak self] in self?.recoverPendingRecordings(recoveryFiles) }
    }

    deinit {
        timer?.invalidate()
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
    }

    func snapshot() -> [String: Any] {
        precondition(Thread.isMainThread)
        metricsLock.lock()
        let seconds = capturedSeconds
        let level = audioLevel
        let configuration = settings
        let mode = captureMode
        let saving = activeSegmentStartedAt != nil
        let segmentElapsed = activeSegmentSeconds
        let activeStart: Any = activeSegmentStartedAt.map { $0.timeIntervalSince1970 * 1000 } ?? NSNull()
        let forced = forcedRecording
        let rms = measuredRMS
        let floor = noiseFloor
        let snr = signalToNoise
        let confidence = speechConfidence
        let detectorStatus = speechStatus
        let detectorError = speechError
        let waveform = waveformRecent
        metricsLock.unlock()
        let capturing = state == "recording" || state == "listening"
        var result: [String: Any] = [
            "state": capturing ? (saving || mode == "manual" ? "recording" : "listening") : state,
            "mode": mode, "listening": capturing && mode == "listening", "recording": capturing && saving,
            "elapsed": seconds, "segmentElapsed": segmentElapsed, "level": capturing ? level : 0,
            "activeStartedAt": activeStart, "forcedRecording": capturing && forced,
            "rms": rms, "noiseFloor": floor, "snr": snr,
            "sensitivity": configuration.sensitivity, "speechDetection": configuration.speechDetection,
            "speechConfidence": confidence, "speechStatus": detectorStatus, "speechError": detectorError,
            "waveformRecent": waveform,
            "inputDevice": inputDevice, "permission": permissionName(),
            "pendingImports": pendingImports, "lastError": lastError,
            "autoTranscribe": configuration.autoTranscribe,
            "silenceSeconds": configuration.silenceSeconds,
            "maxSegmentSeconds": configuration.maxSegmentSeconds,
            "segmentCount": segmentCount,
        ]
        if let lastSaved = lastSaved { result["lastSaved"] = lastSaved }
        return result
    }

    func handleCommand(_ command: String, options: [String: Any] = [:]) {
        precondition(Thread.isMainThread)
        switch command {
        case "start": start()
        case "listen": listen()
        case "stopListening": stop()
        case "record": record()
        case "endSegment": endSegment()
        case "pause": pause()
        case "resume": resume()
        case "stop": stop()
        case "split": split()
        case "settings", "configure":
            metricsLock.lock()
            if let value = options["autoTranscribe"] as? Bool { settings.autoTranscribe = value }
            if let value = options["silenceSeconds"] as? NSNumber, value.doubleValue.isFinite {
                settings.silenceSeconds = value.doubleValue == 0 ? 0 : min(120, max(5, value.doubleValue))
            }
            if let value = options["maxSegmentSeconds"] as? NSNumber, value.doubleValue.isFinite {
                settings.maxSegmentSeconds = min(600, max(30, value.doubleValue))
            }
            if let value = options["sensitivity"] as? NSNumber, value.doubleValue.isFinite {
                settings.sensitivity = min(8, max(1.5, value.doubleValue))
            }
            if let value = options["speechDetection"] as? Bool { settings.speechDetection = value }
            let configuration = settings
            metricsLock.unlock()
            preferences.set(configuration.autoTranscribe, forKey: "nativeRecorder.autoTranscribe")
            preferences.set(configuration.silenceSeconds, forKey: "nativeRecorder.silenceSeconds")
            preferences.set(configuration.maxSegmentSeconds, forKey: "nativeRecorder.maxSegmentSeconds")
            preferences.set(configuration.sensitivity, forKey: "nativeRecorder.sensitivity")
            preferences.set(configuration.speechDetection, forKey: "nativeRecorder.speechDetection")
            emitStatus()
        case "status": emitStatus()
        default: lastError = "未知录音命令：\(command)"; emitStatus()
        }
    }

    func start() {
        startCapture(mode: "manual")
    }

    func listen() {
        startCapture(mode: "listening")
    }

    func record() {
        precondition(Thread.isMainThread)
        guard !terminating else { return }
        guard state != "stopping" else { return }
        if state == "idle" || state == "error" { listen() }
        else if state == "paused" || state == "interrupted" { resume() }
        guard userWantsRecording, state != "stopping" else { return }
        writerQueue.async { [weak self] in
            guard let self = self else { return }
            self.forceNextSegment = true
            self.cooldownUntil = 0
            self.metricsLock.lock()
            self.forcedRecording = true
            self.metricsLock.unlock()
        }
    }

    func endSegment() {
        precondition(Thread.isMainThread)
        guard state == "recording" || state == "listening" else { return }
        metricsLock.lock()
        captureMode = "listening"
        metricsLock.unlock()
        writerQueue.async { [weak self] in
            guard let self = self else { return }
            self.sealSegment(reason: "manual_end_segment")
            self.forceNextSegment = false
            self.continueSegment = false
            self.preRoll.removeAll()
            self.preRollSeconds = 0
            self.activationSeconds = 0
            self.metricsLock.lock()
            self.forcedRecording = false
            self.cooldownUntil = self.capturedSeconds + 1.5
            self.metricsLock.unlock()
        }
    }

    private func startCapture(mode: String) {
        precondition(Thread.isMainThread)
        guard !terminating else { return }
        if state == "paused" || state == "interrupted" {
            metricsLock.lock(); captureMode = mode; metricsLock.unlock()
            resume(); return
        }
        if state == "recording" || state == "listening" {
            metricsLock.lock(); captureMode = mode; metricsLock.unlock()
            emitStatus(); return
        }
        guard state == "idle" || state == "error" else { return }
        operation += 1
        let requestedOperation = operation
        userWantsRecording = true
        manuallyPaused = false
        lastError = ""
        segmentCount = 0
        metricsLock.lock()
        capturedSeconds = 0
        audioLevel = 0
        captureMode = mode
        waveformRecent.removeAll()
        noiseFloor = 0.0015
        measuredRMS = 0
        signalToNoise = 0
        speechStatus = mode == "listening" && settings.speechDetection ? "warming" : "disabled"
        speechError = ""
        metricsLock.unlock()
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: beginCapture()
        case .notDetermined:
            state = "requesting"
            emitStatus()
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] allowed in
                DispatchQueue.main.async {
                    guard let self = self, self.operation == requestedOperation,
                          self.userWantsRecording, !self.terminating else { return }
                    if allowed { self.beginCapture() }
                    else { self.fail("麦克风权限未获允许，请在系统设置 → 隐私与安全性 → 麦克风中允许 Fennec。") }
                }
            }
        case .denied, .restricted:
            fail("麦克风权限未获允许，请在系统设置 → 隐私与安全性 → 麦克风中允许 Fennec。")
        @unknown default: fail("无法确认麦克风权限。")
        }
    }

    func pause() {
        precondition(Thread.isMainThread)
        guard userWantsRecording, !terminating else { return }
        manuallyPaused = true
        resumeAfterSeal = false
        operation += 1
        sealCapture(reason: "manual_pause", nextState: "paused")
    }

    func resume() {
        precondition(Thread.isMainThread)
        guard !terminating else { return }
        if state == "idle" || state == "error" {
            metricsLock.lock(); let mode = captureMode; metricsLock.unlock()
            startCapture(mode: mode); return
        }
        guard userWantsRecording, state == "paused" || state == "interrupted" || state == "stopping" else { return }
        manuallyPaused = false
        if state == "stopping" { resumeAfterSeal = true; return }
        guard !sleeping else { state = "interrupted"; emitStatus(); return }
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            fail("麦克风权限已关闭，请在系统设置中重新允许后开始录音。")
            return
        }
        beginCapture()
    }

    func stop() {
        precondition(Thread.isMainThread)
        userWantsRecording = false
        manuallyPaused = false
        resumeAfterSeal = false
        operation += 1
        sealCapture(reason: "manual_stop", nextState: "idle")
    }

    func split() {
        precondition(Thread.isMainThread)
        guard state == "recording" || state == "listening" else { return }
        writerQueue.async { [weak self] in
            guard let self = self, self.audioFile != nil else { return }
            self.sealSegment(reason: "manual_split")
            self.continueSegment = true
        }
    }

    func prepareForTermination(completion: @escaping () -> Void) {
        precondition(Thread.isMainThread)
        terminating = true
        timer?.invalidate()
        userWantsRecording = false
        resumeAfterSeal = false
        operation += 1
        sealCapture(reason: "application_exit", nextState: "idle", completion: completion)
    }

    private func beginCapture() {
        guard userWantsRecording, !manuallyPaused, !sleeping, !terminating else { return }
        do {
            try FileManager.default.createDirectory(at: recordingsDirectory, withIntermediateDirectories: true)
            let newEngine = AVAudioEngine()
            let input = newEngine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw RecorderError(message: "没有可用的默认输入设备。")
            }
            metricsLock.lock()
            captureGeneration += 1
            let generation = captureGeneration
            acceptingAudio = true
            lastBufferTime = ProcessInfo.processInfo.systemUptime
            metricsLock.unlock()
            engine = newEngine
            input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
                self?.receive(buffer, generation: generation)
            }
            tapInstalled = true
            newEngine.prepare()
            try newEngine.start()
            inputDevice = Self.defaultInputDeviceName()
            captureStartedAt = Date()
            state = "recording"
            lastError = ""
            emitStatus()
        } catch {
            // Treat an unavailable/hot-unplugged device as a recoverable interruption.
            lastError = "无法启动麦克风：\(error.localizedDescription)"
            sealCapture(reason: "input_unavailable", nextState: "interrupted")
            nextRestartAt = Date().addingTimeInterval(5)
        }
    }

    private func receive(_ buffer: AVAudioPCMBuffer, generation: Int) {
        let receivedAt = Date().addingTimeInterval(-Double(buffer.frameLength) / buffer.format.sampleRate)
        metricsLock.lock()
        let accepted = acceptingAudio && captureGeneration == generation
        if accepted { lastBufferTime = ProcessInfo.processInfo.systemUptime }
        metricsLock.unlock()
        guard accepted, buffer.frameLength > 0 else { return }
        guard bufferSlots.wait(timeout: .now()) == .success else {
            rejectAudio(generation: generation, message: "磁盘写入跟不上录音，已停止采集并保存已有声音。请检查磁盘后重新开始。")
            return
        }
        guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
            bufferSlots.signal()
            rejectAudio(generation: generation, message: "无法分配音频缓冲区，已停止采集并保存已有声音。")
            return
        }
        copy.frameLength = buffer.frameLength
        let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        guard source.count == destination.count else {
            bufferSlots.signal()
            rejectAudio(generation: generation, message: "输入音频格式变化，已停止采集并保留录音。")
            return
        }
        for index in 0..<min(source.count, destination.count) {
            guard source[index].mDataByteSize <= destination[index].mDataByteSize else {
                bufferSlots.signal()
                rejectAudio(generation: generation, message: "输入音频缓冲区大小异常，已停止采集并保留录音。")
                return
            }
            if let from = source[index].mData, let to = destination[index].mData {
                memcpy(to, from, Int(source[index].mDataByteSize))
            }
        }
        // No file access, encoding, RMS scan, or unbounded queueing in the audio callback.
        // Enqueue under the same short lock used to disable capture. A late callback
        // cannot enqueue behind the stop barrier and accidentally open another file.
        metricsLock.lock()
        guard acceptingAudio && captureGeneration == generation else {
            metricsLock.unlock()
            bufferSlots.signal()
            return
        }
        writerQueue.async { [weak self] in
            guard let self = self else { return }
            defer { self.bufferSlots.signal() }
            self.write(copy, receivedAt: receivedAt)
        }
        metricsLock.unlock()
    }

    private func rejectAudio(generation: Int, message: String) {
        metricsLock.lock()
        let shouldReport = acceptingAudio && captureGeneration == generation
        if shouldReport { acceptingAudio = false }
        metricsLock.unlock()
        if shouldReport {
            DispatchQueue.main.async { [weak self] in self?.fail(message) }
        }
    }

    private func write(_ buffer: AVAudioPCMBuffer, receivedAt: Date) {
        do {
            let seconds = Double(buffer.frameLength) / buffer.format.sampleRate
            let rms = Self.rms(buffer)
            metricsLock.lock()
            let configuration = settings
            let mode = captureMode
            capturedSeconds += seconds
            let elapsed = capturedSeconds
            metricsLock.unlock()

            let detection = classification(for: buffer, configuration: configuration, mode: mode)
            updateNoise(rms: rms, seconds: seconds, elapsed: elapsed, detection: detection, configuration: configuration)
            metricsLock.lock()
            let floor = noiseFloor
            let forced = forcedRecording
            measuredRMS = rms
            signalToNoise = rms / max(floor, 0.000001)
            audioLevel = rms > 0 ? min(1, max(0, (20 * log10(rms) + 60) / 60)) : 0
            speechConfidence = detection.confidence
            speechStatus = detection.state
            speechError = detection.error
            if receivedAt.timeIntervalSince(lastWaveformTime) >= 0.5 {
                lastWaveformTime = receivedAt
                waveformRecent.append(["t": receivedAt.timeIntervalSince1970 * 1000, "level": audioLevel])
                waveformRecent.removeAll { ($0["t"] ?? 0) < receivedAt.addingTimeInterval(-60).timeIntervalSince1970 * 1000 }
                if waveformRecent.count > 120 { waveformRecent.removeFirst(waveformRecent.count - 120) }
            }
            metricsLock.unlock()

            let aboveNoise = rms >= max(0.001, floor * configuration.sensitivity)
            // Classification refers to a past one-second window. A short word may
            // already have ended by the time its result arrives: consult pre-roll
            // energy while waiting, rather than requiring sound in this instant.
            let recentRMS = audioFile == nil ? max(rms, preRoll.map { $0.rms }.max() ?? 0) : rms
            let voiceActive: Bool
            if configuration.speechDetection && mode == "listening" && detection.state == "ready" {
                let confidenceThreshold = min(0.65, 0.28 + (configuration.sensitivity - 1.5) * 0.05)
                voiceActive = detection.confidence >= confidenceThreshold &&
                    (aboveNoise || recentRMS >= max(0.001, floor * configuration.sensitivity) ||
                     (detection.confidence >= 0.8 && recentRMS > 0.0003))
            } else if configuration.speechDetection && mode == "listening" && detection.state == "warming" {
                voiceActive = false
            } else {
                // Explicitly labelled volume-only mode (disabled or degraded classifier).
                voiceActive = aboveNoise && elapsed >= 0.5
            }
            activationSeconds = voiceActive ? activationSeconds + seconds : 0

            if mode == "listening" && audioFile == nil {
                try appendPreRoll(buffer, at: receivedAt, seconds: seconds, rms: rms)
                let shouldOpen = forceNextSegment || continueSegment ||
                    (elapsed >= cooldownUntil && activationSeconds >= 0.1)
                guard shouldOpen else { return }
                if continueSegment {
                    // Split/max-duration continuation must contain exactly the next
                    // buffer, never duplicate the previous segment's pre-roll.
                    preRoll = [PreRollChunk(buffer: buffer, at: receivedAt, seconds: seconds, rms: rms)]
                    preRollSeconds = seconds
                }
                let chunks = preRoll
                preRoll.removeAll(keepingCapacity: true)
                preRollSeconds = 0
                try openSegment(format: buffer.format, at: chunks.first?.at ?? receivedAt)
                for chunk in chunks { try writeSegmentBuffer(chunk.buffer) }
                forceNextSegment = false
                continueSegment = false
            } else {
                if audioFile == nil { try openSegment(format: buffer.format, at: receivedAt) }
                try writeSegmentBuffer(buffer)
            }

            if mode == "manual" { quietSeconds = rms < 0.004 ? quietSeconds + seconds : 0 }
            else { quietSeconds = voiceActive || forced ? 0 : quietSeconds + seconds }
            if segmentSeconds >= min(600, configuration.maxSegmentSeconds) {
                sealSegment(reason: "maximum_duration")
                continueSegment = mode == "listening"
            } else if configuration.silenceSeconds > 0 && quietSeconds >= configuration.silenceSeconds && !forced {
                sealSegment(reason: mode == "listening" ? "silence_end" : "low_energy")
                continueSegment = false
                activationSeconds = 0
            }
        } catch {
            metricsLock.lock()
            let shouldReport = acceptingAudio
            acceptingAudio = false
            metricsLock.unlock()
            if shouldReport {
                DispatchQueue.main.async { [weak self] in self?.fail("录音写入失败，原始文件已保留：\(error.localizedDescription)") }
            }
        }
    }

    #if RECORDER_TESTING
    private var testSpeechStatus: NativeSpeechDetector.Status?
    #endif

    private func classification(for buffer: AVAudioPCMBuffer, configuration: Settings, mode: String) -> NativeSpeechDetector.Status {
        guard mode == "listening", configuration.speechDetection else {
            speechDetector?.cancel()
            speechDetector = nil
            analysisFramePosition = 0
            return NativeSpeechDetector.Status(state: "disabled")
        }
        #if RECORDER_TESTING
        if let overridden = testSpeechStatus { return overridden }
        #endif
        if speechDetector == nil {
            speechDetector = NativeSpeechDetector(format: buffer.format)
            analysisFramePosition = 0
        }
        speechDetector?.submit(buffer, framePosition: analysisFramePosition)
        analysisFramePosition += AVAudioFramePosition(buffer.frameLength)
        return speechDetector?.snapshot(at: Double(analysisFramePosition) / buffer.format.sampleRate)
            ?? NativeSpeechDetector.Status(state: "unavailable", error: "人声检测不可用，退化为音量检测。")
    }

    private func updateNoise(rms: Double, seconds: Double, elapsed: Double,
                             detection: NativeSpeechDetector.Status, configuration: Settings) {
        metricsLock.lock()
        let previous = noiseFloor
        metricsLock.unlock()
        let knownQuiet = detection.state == "ready" && detection.confidence < 0.2
        let volumeMode = !configuration.speechDetection || ["disabled", "unavailable", "stale"].contains(detection.state)
        // Calibrate volume-only mode from its first half-second, then adapt slowly
        // upward and quickly downward while waiting. Confirmed speech never raises
        // the floor. This is an energy estimate, not a substitute speech classifier.
        if volumeMode && elapsed <= 0.6 {
            noiseSamples.append(rms)
            if noiseSamples.count > 64 { noiseSamples.removeFirst() }
            let sorted = noiseSamples.sorted()
            let candidate = sorted[max(0, sorted.count / 5)]
            metricsLock.lock(); noiseFloor = max(0.0001, candidate); metricsLock.unlock()
        } else if rms < previous || knownQuiet || (volumeMode && audioFile == nil && rms < previous * configuration.sensitivity) {
            let timeConstant = rms < previous ? 1.5 : (knownQuiet ? 4.0 : 20.0)
            let alpha = 1 - exp(-seconds / timeConstant)
            metricsLock.lock()
            noiseFloor = max(0.0001, previous + alpha * (rms - previous))
            metricsLock.unlock()
        }
    }

    private func appendPreRoll(_ buffer: AVAudioPCMBuffer, at: Date, seconds: Double, rms: Double) throws {
        var chunk = PreRollChunk(buffer: buffer, at: at, seconds: seconds, rms: rms)
        // Coalesce tiny device callbacks on the writer queue. A fixed buffer-count
        // cap alone would silently turn "2 seconds" into a fraction of a second on
        // low-latency hardware. Normalized chunks keep both duration and object
        // overhead bounded, without copying or mutating the analyzer's input.
        if let previous = preRoll.last, previous.seconds < 0.1, previous.buffer.format.isEqual(buffer.format) {
            let frames = previous.buffer.frameLength + buffer.frameLength
            guard let combined = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: frames) else {
                throw RecorderError(message: "无法分配监听预卷缓冲区。")
            }
            combined.frameLength = frames
            let first = UnsafeMutableAudioBufferListPointer(previous.buffer.mutableAudioBufferList)
            let second = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            let output = UnsafeMutableAudioBufferListPointer(combined.mutableAudioBufferList)
            for index in 0..<output.count {
                guard let destination = output[index].mData, let fromFirst = first[index].mData,
                      let fromSecond = second[index].mData else { continue }
                memcpy(destination, fromFirst, Int(first[index].mDataByteSize))
                memcpy(destination.advanced(by: Int(first[index].mDataByteSize)), fromSecond, Int(second[index].mDataByteSize))
            }
            preRoll.removeLast()
            preRollSeconds -= previous.seconds
            chunk = PreRollChunk(buffer: combined, at: previous.at, seconds: previous.seconds + seconds, rms: max(previous.rms, rms))
        }
        preRoll.append(chunk)
        preRollSeconds += chunk.seconds
        while preRoll.count > 1 && preRollSeconds - preRoll[0].seconds >= 2 {
            preRollSeconds -= preRoll.removeFirst().seconds
        }
    }

    private func writeSegmentBuffer(_ buffer: AVAudioPCMBuffer) throws {
        guard let file = audioFile else { throw RecorderError(message: "音频文件未打开。") }
        try file.write(from: buffer)
        segmentSeconds += Double(buffer.frameLength) / buffer.format.sampleRate
        metricsLock.lock()
        activeSegmentSeconds = segmentSeconds
        metricsLock.unlock()
    }

    private func openSegment(format: AVAudioFormat, at date: Date) throws {
        let id = UUID().uuidString.lowercased()
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let stem = "rec_\(formatter.string(from: date))_\(id)"
        metricsLock.lock()
        let autoTranscribe = settings.autoTranscribe
        metricsLock.unlock()
        let manifest = Manifest(captureID: id, stem: stem, startedAt: date, autoTranscribe: autoTranscribe, phase: "capturing")
        // Persist identity before opening the raw file: a crash cannot leave unidentifiable audio.
        try saveManifest(manifest)
        var fileSettings = format.settings
        fileSettings[AVLinearPCMIsNonInterleaved] = false
        let file = try AVAudioFile(forWriting: url(stem, "caf"), settings: fileSettings,
                                   commonFormat: format.commonFormat, interleaved: format.isInterleaved)
        segment = manifest
        audioFile = file
        segmentSeconds = 0
        quietSeconds = 0
        metricsLock.lock()
        activeSegmentStartedAt = date
        activeSegmentSeconds = 0
        metricsLock.unlock()
        DispatchQueue.main.async { [weak self] in self?.segmentCount += 1 }
    }

    private func sealSegment(reason: String) {
        // Releasing AVAudioFile closes/finalizes its CAF header before conversion.
        audioFile = nil
        metricsLock.lock()
        activeSegmentStartedAt = nil
        activeSegmentSeconds = 0
        metricsLock.unlock()
        guard var closed = segment else { return }
        segment = nil
        metricsLock.lock()
        closed.autoTranscribe = settings.autoTranscribe
        metricsLock.unlock()
        closed.endedAt = closed.startedAt.addingTimeInterval(segmentSeconds)
        closed.interruptionReason = reason
        closed.phase = "encoding"
        segmentSeconds = 0
        quietSeconds = 0
        do {
            try saveManifest(closed)
            let manifest = closed
            deliveryQueue.async { [weak self] in self?.encodeAndEnqueue(manifest) }
        } catch {
            DispatchQueue.main.async { [weak self] in
                self?.lastError = "录音原始文件已保留，但保存恢复记录失败：\(error.localizedDescription)"
                self?.emitStatus()
            }
        }
    }

    private func sealCapture(reason: String, nextState: String, completion: (() -> Void)? = nil) {
        state = "stopping"
        let expectedOperation = operation
        metricsLock.lock()
        acceptingAudio = false
        audioLevel = 0
        metricsLock.unlock()
        if let engine = engine {
            engine.stop()
            if tapInstalled { engine.inputNode.removeTap(onBus: 0) }
        }
        engine = nil
        tapInstalled = false
        emitStatus()
        writerQueue.async { [weak self] in
            if let self = self {
                self.sealSegment(reason: reason)
                self.preRoll.removeAll()
                self.preRollSeconds = 0
                self.forceNextSegment = false
                self.continueSegment = false
                self.activationSeconds = 0
                self.cooldownUntil = 0
                self.noiseSamples.removeAll()
                self.speechDetector?.cancel()
                self.speechDetector = nil
                self.analysisFramePosition = 0
                self.metricsLock.lock()
                self.forcedRecording = false
                self.speechStatus = "disabled"
                self.speechConfidence = 0
                self.speechError = ""
                self.metricsLock.unlock()
            }
            DispatchQueue.main.async {
                guard let self = self else { completion?(); return }
                if self.operation == expectedOperation {
                    self.state = nextState
                    self.emitStatus()
                    if self.resumeAfterSeal && self.userWantsRecording && !self.manuallyPaused && !self.sleeping && !self.terminating {
                        self.resumeAfterSeal = false
                        self.beginCapture()
                    }
                }
                completion?()
            }
        }
    }

    private func fail(_ message: String) {
        userWantsRecording = false
        resumeAfterSeal = false
        lastError = message
        operation += 1
        sealCapture(reason: "capture_error", nextState: "error")
    }

    private func interrupt(reason: String, message: String) {
        guard userWantsRecording, !manuallyPaused else { return }
        operation += 1
        lastError = message
        nextRestartAt = Date().addingTimeInterval(2)
        sealCapture(reason: reason, nextState: "interrupted")
    }

    private func willSleep() {
        sleeping = true
        if userWantsRecording && !manuallyPaused {
            interrupt(reason: "system_sleep", message: "系统睡眠，录音已保存；唤醒后恢复。")
        }
    }

    private func didWake() {
        sleeping = false
        guard userWantsRecording, !manuallyPaused, !terminating else { return }
        nextRestartAt = Date().addingTimeInterval(1)
        if state == "stopping" { resumeAfterSeal = true }
    }

    private func tick() {
        if state == "recording" || state == "listening" {
            metricsLock.lock()
            let lastBuffer = lastBufferTime
            metricsLock.unlock()
            if ProcessInfo.processInfo.systemUptime - lastBuffer > 8 && Date().timeIntervalSince(captureStartedAt) > 8 {
                interrupt(reason: "no_audio_buffers", message: "麦克风超过 8 秒未提供音频，已封口并尝试重新连接。")
            }
        } else if state == "interrupted" && userWantsRecording && !manuallyPaused && !sleeping && !terminating && Date() >= nextRestartAt {
            nextRestartAt = Date().addingTimeInterval(5)
            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                fail("麦克风权限不可用，请重新允许后开始录音。")
                return
            }
            beginCapture()
        }
        emitStatus()
    }

    private func emitStatus() { onStatus?(snapshot()) }

    private func permissionName() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return "authorized"
        case .notDetermined: return "notDetermined"
        case .denied: return "denied"
        case .restricted: return "restricted"
        @unknown default: return "unknown"
        }
    }

    private func url(_ stem: String, _ ext: String) -> URL {
        recordingsDirectory.appendingPathComponent(stem).appendingPathExtension(ext)
    }

    private func saveManifest(_ manifest: Manifest) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var value = encoder.singleValueContainer()
            try value.encode(Self.isoDate(date))
        }
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(manifest).write(to: url(manifest.stem, "json"), options: .atomic)
    }

    private func encodeAndEnqueue(_ initial: Manifest) {
        var manifest = initial
        pending[manifest.captureID] = manifest
        reportPendingCount()
        do {
            let finalURL = url(manifest.stem, "m4a")
            if !FileManager.default.fileExists(atPath: finalURL.path) {
                let rawURL = url(manifest.stem, "caf")
                let raw = try AVAudioFile(forReading: rawURL)
                guard raw.length > 0, raw.processingFormat.sampleRate > 0 else {
                    throw RecorderError(message: "原始录音无可解码音频，文件已保留供检查。")
                }
                let duration = Double(raw.length) / raw.processingFormat.sampleRate
                if manifest.endedAt == nil { manifest.endedAt = manifest.startedAt.addingTimeInterval(duration) }
                let temporary = url(manifest.stem + ".encoding-" + UUID().uuidString, "m4a")
                let converter = Process()
                converter.executableURL = URL(fileURLWithPath: "/usr/bin/afconvert")
                converter.arguments = [rawURL.path, temporary.path, "-f", "m4af", "-d", "aac@16000", "-c", "1", "-b", "24000"]
                converter.standardOutput = FileHandle.nullDevice
                converter.standardError = FileHandle.nullDevice
                try converter.run()
                let deadline = Date().addingTimeInterval(max(60, duration))
                while converter.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
                if converter.isRunning {
                    converter.terminate()
                    throw RecorderError(message: "音频编码超时，原始 CAF 已保留。")
                }
                guard converter.terminationStatus == 0 else {
                    throw RecorderError(message: "音频编码失败（\(converter.terminationStatus)），原始 CAF 已保留。")
                }
                _ = try validateAudio(temporary)
                // Same-directory rename exposes only a fully validated final file.
                try FileManager.default.moveItem(at: temporary, to: finalURL)
            }
            let finalDuration = try validateAudio(finalURL)
            // Recover the rename-before-manifest-write crash window, including old
            // manifests whose subsecond start/end dates were rounded to one second.
            if manifest.endedAt == nil || manifest.endedAt! <= manifest.startedAt {
                manifest.endedAt = manifest.startedAt.addingTimeInterval(finalDuration)
            }
            manifest.phase = "pending_import"
            manifest.error = nil
            try saveManifest(manifest)
            pending[manifest.captureID] = manifest
            // Raw files from interruptions stay recoverable even after successful encoding.
            if ["manual_stop", "manual_pause", "manual_split", "manual_end_segment", "maximum_duration", "low_energy", "silence_end", "application_exit"].contains(manifest.interruptionReason ?? "") {
                try? FileManager.default.removeItem(at: url(manifest.stem, "caf"))
            }
            let savedName = finalURL.lastPathComponent
            DispatchQueue.main.async { [weak self] in self?.lastSaved = savedName; self?.emitStatus() }
            deliverNext()
        } catch {
            manifest.phase = "recovery_required"
            manifest.error = error.localizedDescription
            try? saveManifest(manifest)
            pending[manifest.captureID] = manifest
            reportDeliveryError("录音待恢复：\(error.localizedDescription)")
        }
    }

    private func validateAudio(_ fileURL: URL) throws -> Double {
        let audio = try AVAudioFile(forReading: fileURL)
        guard audio.length > 0, audio.processingFormat.sampleRate > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: 1024) else {
            throw RecorderError(message: "编码后的录音无有效音频。")
        }
        try audio.read(into: buffer)
        guard buffer.frameLength > 0 else { throw RecorderError(message: "编码后的录音无法解码。") }
        return Double(audio.length) / audio.processingFormat.sampleRate
    }

    private func deliverNext() {
        guard !importing else { return }
        guard let manifest = pending.values.filter({ $0.phase == "pending_import" }).sorted(by: { $0.startedAt < $1.startedAt }).first else { return }
        importing = true
        var body: [String: Any] = [
            "path": url(manifest.stem, "m4a").path,
            "capture_id": manifest.captureID,
            "started_at": Self.isoDate(manifest.startedAt),
            "ended_at": Self.isoDate(manifest.endedAt ?? manifest.startedAt),
            "auto_transcribe": manifest.autoTranscribe,
        ]
        if let reason = manifest.interruptionReason { body["interruption_reason"] = reason }
        var request = URLRequest(url: baseURL.appendingPathComponent("api/recordings/import"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 20
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self else { return }
            self.deliveryQueue.async {
                self.importing = false
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if error == nil, (200..<300).contains(code), json?["id"] != nil {
                    // A durable tombstone prevents retained interrupted CAF files from
                    // reappearing if the user later deletes the imported m4a session.
                    var delivered = manifest
                    delivered.phase = "delivered"
                    delivered.error = nil
                    do {
                        try self.saveManifest(delivered)
                        self.pending.removeValue(forKey: manifest.captureID)
                        self.retryDelay = 2
                        self.reportPendingCount()
                        self.deliverNext()
                    } catch {
                        self.reportDeliveryError("录音已入库，但保存送达标记失败；将安全重试：\(error.localizedDescription)", transient: true)
                        self.scheduleRetry()
                    }
                } else if error == nil && (400..<500).contains(code) && ![408, 425, 429].contains(code) {
                    // A permanently rejected item must not starve every later recording.
                    var rejected = manifest
                    rejected.phase = "recovery_required"
                    rejected.error = "入库被拒绝（HTTP \(code)），音频已保留，需检查此录音。"
                    try? self.saveManifest(rejected)
                    self.pending[manifest.captureID] = rejected
                    self.reportDeliveryError(rejected.error!)
                    self.reportPendingCount()
                    self.deliverNext()
                } else {
                    self.reportDeliveryError("录音已保存在本机，等待入库（\(error?.localizedDescription ?? "HTTP \(code)")）。", transient: true)
                    self.scheduleRetry()
                }
            }
        }.resume()
    }

    private func scheduleRetry() {
        guard !retryScheduled else { return }
        retryScheduled = true
        let delay = retryDelay
        retryDelay = min(60, retryDelay * 2)
        deliveryQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.retryScheduled = false
            self?.deliverNext()
        }
    }

    private func recoverPendingRecordings(_ files: [URL]) {
        do {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .custom { decoder in
                let container = try decoder.singleValueContainer()
                let value = try container.decode(String.self)
                let fractional = ISO8601DateFormatter()
                fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                if let date = fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value) { return date }
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid recording timestamp")
            }
            var knownStems = Set<String>()
            for file in files where file.lastPathComponent.hasPrefix("rec_") && file.pathExtension == "json" {
                do {
                    var manifest = try decoder.decode(Manifest.self, from: Data(contentsOf: file))
                    // Reject traversal in damaged/hand-edited manifests.
                    guard manifest.stem == file.deletingPathExtension().lastPathComponent,
                          !manifest.stem.contains("/"), !manifest.stem.contains("..") else {
                        throw RecorderError(message: "恢复记录中的文件名无效。")
                    }
                    knownStems.insert(manifest.stem)
                    if manifest.phase == "delivered" { continue }
                    if manifest.phase == "capturing" {
                        manifest.interruptionReason = "recovered_after_exit"
                        manifest.phase = "encoding"
                    }
                    encodeAndEnqueue(manifest)
                } catch {
                    reportDeliveryError("发现无法读取的录音恢复记录，文件已保留：\(file.lastPathComponent)")
                }
            }
            for file in files where file.lastPathComponent.hasPrefix("rec_") && file.pathExtension == "caf" {
                let stem = file.deletingPathExtension().lastPathComponent
                guard !knownStems.contains(stem), !FileManager.default.fileExists(atPath: url(stem, "m4a").path) else { continue }
                let created = (try? file.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
                let manifest = Manifest(captureID: UUID().uuidString.lowercased(), stem: stem,
                                        startedAt: created, interruptionReason: "recovered_orphan_caf",
                                        autoTranscribe: false, phase: "encoding")
                try saveManifest(manifest)
                encodeAndEnqueue(manifest)
            }
            reportPendingCount()
            deliverNext()
        } catch {
            reportDeliveryError("无法检查待恢复录音：\(error.localizedDescription)")
        }
    }

    private func reportPendingCount() {
        let count = pending.count
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            self.pendingImports = count
            if count == 0 {
                if self.lastError == self.lastDeliveryError { self.lastError = "" }
                self.lastDeliveryError = nil
            }
            self.emitStatus()
        }
    }

    private func reportDeliveryError(_ message: String, transient: Bool = false) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if transient { self.lastDeliveryError = message }
            // A temporary offline backend must not overwrite the reason capture stopped.
            if self.state != "error" || self.lastError.isEmpty { self.lastError = message }
            self.emitStatus()
        }
    }

    private static func isoDate(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    private static func rms(_ buffer: AVAudioPCMBuffer) -> Double {
        guard let channels = buffer.floatChannelData else { return 1 }
        let frames = Int(buffer.frameLength)
        guard frames > 0 else { return 0 }
        let channelCount = Int(buffer.format.channelCount)
        var sum: Double = 0
        if buffer.format.isInterleaved {
            for index in 0..<(frames * channelCount) { let value = Double(channels[0][index]); sum += value * value }
        } else {
            for channel in 0..<channelCount {
                for index in 0..<frames { let value = Double(channels[channel][index]); sum += value * value }
            }
        }
        return sqrt(sum / Double(frames * channelCount))
    }

    private static func defaultInputDeviceName() -> String {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr,
              device != kAudioObjectUnknown else { return "未找到默认麦克风" }
        address.mSelector = kAudioObjectPropertyName
        var name: Unmanaged<CFString>?
        size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name) == noErr,
              let name = name else { return "默认麦克风" }
        return name.takeUnretainedValue() as String
    }
}
