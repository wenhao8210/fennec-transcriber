import AVFoundation
import SoundAnalysis

/// Apple's on-device sound classifier. This type never opens an audio device.
/// Caller supplies immutable native-format PCM; the bounded analysis queue is
/// independent of recording IO and cannot hold up the audio writer.
final class NativeSpeechDetector {
    struct Status {
        var confidence: Double = 0
        var analyzedThrough: Double = 0
        var state = "warming"
        var error = ""
        var label = ""
    }

    private let queue = DispatchQueue(label: "fennec.recorder.speech-analysis", qos: .userInitiated)
    private let slots = DispatchSemaphore(value: 24)
    private let lock = NSLock()
    private var status = Status()
    private var cancelled = false
    private var analyzer: SNAudioStreamAnalyzer?
    private var observer: Observer?
    private var speechLabels = Set<String>()
    private let sampleRate: Double

    init(format: AVAudioFormat) {
        sampleRate = format.sampleRate
        queue.async { [weak self] in
            guard let self = self else { return }
            do {
                let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
                request.windowDuration = CMTime(seconds: 1, preferredTimescale: 48_000)
                request.overlapFactor = 0.5
                let labels = request.knownClassifications.filter { label in
                    let value = label.lowercased()
                    return value.contains("speech") || value.contains("conversation") || value.contains("whisper") || value.contains("narration")
                }
                guard !labels.isEmpty else { throw DetectorError(message: "系统声音分类器没有可用的人声标签。") }
                self.speechLabels = Set(labels)
                let analyzer = SNAudioStreamAnalyzer(format: format)
                let observer = Observer(owner: self)
                try analyzer.add(request, withObserver: observer)
                self.analyzer = analyzer
                self.observer = observer
            } catch {
                self.updateFailure("系统人声检测不可用：\(error.localizedDescription)")
            }
        }
    }

    func submit(_ buffer: AVAudioPCMBuffer, framePosition: AVAudioFramePosition) {
        lock.lock()
        let active = !cancelled && status.state != "unavailable"
        lock.unlock()
        guard active else { return }
        guard slots.wait(timeout: .now()) == .success else {
            lock.lock()
            status.state = "stale"
            status.error = "人声分类处理滞后，暂时退化为音量检测。"
            lock.unlock()
            return
        }
        let slots = self.slots
        queue.async { [weak self] in
            defer { slots.signal() }
            guard let self = self else { return }
            self.lock.lock()
            let active = !self.cancelled
            self.lock.unlock()
            if active { self.analyzer?.analyze(buffer, atAudioFramePosition: framePosition) }
        }
    }

    func snapshot(at audioSeconds: Double) -> Status {
        lock.lock()
        var result = status
        lock.unlock()
        if result.state != "unavailable" && audioSeconds - result.analyzedThrough > 2.5 {
            result.state = "stale"
            result.error = "人声分类超过 2.5 秒未跟上采集，暂时退化为音量检测。"
        }
        return result
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
        queue.async { [self] in analyzer?.removeAllRequests(); analyzer = nil; observer = nil }
    }

    private func updateFailure(_ message: String) {
        lock.lock()
        status.state = "unavailable"
        status.error = message + " 暂时退化为音量检测。"
        lock.unlock()
    }

    private func receive(_ result: SNClassificationResult) {
        let speech = result.classifications.filter { speechLabels.contains($0.identifier) }.max { $0.confidence < $1.confidence }
        lock.lock()
        if !cancelled {
            status.confidence = speech?.confidence ?? 0
            status.label = speech?.identifier ?? ""
            status.analyzedThrough = CMTimeGetSeconds(CMTimeRangeGetEnd(result.timeRange))
            status.state = "ready"
            status.error = ""
        }
        lock.unlock()
    }

    private final class Observer: NSObject, SNResultsObserving {
        weak var owner: NativeSpeechDetector?
        init(owner: NativeSpeechDetector) { self.owner = owner }
        func request(_ request: SNRequest, didProduce result: SNResult) {
            if let result = result as? SNClassificationResult { owner?.receive(result) }
        }
        func request(_ request: SNRequest, didFailWithError error: Error) {
            owner?.updateFailure("系统人声分类失败：\(error.localizedDescription)")
        }
        func requestDidComplete(_ request: SNRequest) {}
    }

    private struct DetectorError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
}
