// recorder — the Meetings tab's audio capture. No bot joins the call: it records on this Mac.
//
// Writes two tracks into the folder given as argv[1], on one shared timeline:
//   me.m4a   — the microphone (your side)
//   them.m4a — everything the Mac plays (the other side of the call)
// Both are 16 kHz mono AAC. Prints "recording" once capture is running, then records
// until SIGINT/SIGTERM or until the app that started it exits.
//
// Build: swiftc -O recorder.swift -o recorder   (macOS 15+, needed for microphone capture)
import AVFoundation
import Foundation
import ScreenCaptureKit

// Every buffer is converted to this, so switching mics mid-call (e.g. AirPods) can't
// break the file, and it's what whisper wants anyway.
let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
let t0 = CMClockGetTime(CMClockGetHostTimeClock())   // recording start, shared by both tracks

final class Track {
  let file: AVAudioFile
  var converter: AVAudioConverter?
  var written: AVAudioFramePosition = 0   // frames in the file so far
  var firstPTS: CMTime?
  var lead = 0.0                          // seconds between t0 and this track's first buffer

  init(url: URL) throws {
    file = try AVAudioFile(forWriting: url, settings: [
      AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1,
    ], commonFormat: .pcmFormatFloat32, interleaved: false)
  }

  // Seconds since t0 for a buffer. Measured from this track's own first buffer, so it
  // holds even if the mic and system audio stamp time on different clocks.
  func position(_ pts: CMTime) -> Double {
    if firstPTS == nil {
      firstPTS = pts
      lead = (CMClockGetTime(CMClockGetHostTimeClock()) - t0).seconds
    }
    return lead + (pts - firstPTS!).seconds
  }

  func append(_ buf: AVAudioPCMBuffer, at seconds: Double) throws {
    // Fill any gap (late start, or no audio delivered for a while) with silence so
    // Me and Them stay aligned for the merge. Jitter under 0.5 s is ignored.
    let gap = AVAudioFramePosition(seconds * target.sampleRate) - written
    if gap > 8000 { try write(silence(AVAudioFrameCount(gap))) }

    if converter?.inputFormat != buf.format { converter = AVAudioConverter(from: buf.format, to: target) }
    let cap = AVAudioFrameCount(Double(buf.frameLength) * target.sampleRate / buf.format.sampleRate) + 512
    guard let conv = converter, let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
    var fed = false
    var err: NSError?
    conv.convert(to: out, error: &err) { _, status in
      if fed { status.pointee = .noDataNow; return nil }
      fed = true
      status.pointee = .haveData
      return buf
    }
    if let err { throw err }
    if out.frameLength > 0 { try write(out) }
  }

  func write(_ b: AVAudioPCMBuffer) throws {
    try file.write(from: b)
    written += AVAudioFramePosition(b.frameLength)
  }

  func silence(_ frames: AVAudioFrameCount) -> AVAudioPCMBuffer {
    let b = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: frames)!
    b.frameLength = frames
    memset(b.floatChannelData![0], 0, Int(frames) * MemoryLayout<Float>.size)
    return b
  }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
  let queue = DispatchQueue(label: "recorder")   // all buffers + file writes happen here
  var tracks: [SCStreamOutputType: Track] = [:]
  var stream: SCStream?
  var stopping = false

  func start(dir: URL) async throws {
    // Asking for shareable content is what triggers the Screen & System Audio Recording prompt.
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    guard let display = content.displays.first else { throw NSError(domain: "recorder", code: 1) }
    tracks = [.microphone: try Track(url: dir.appendingPathComponent("me.m4a")),
              .audio: try Track(url: dir.appendingPathComponent("them.m4a"))]

    let cfg = SCStreamConfiguration()
    cfg.capturesAudio = true
    cfg.captureMicrophone = true
    cfg.excludesCurrentProcessAudio = true
    cfg.sampleRate = 16000
    cfg.channelCount = 1
    cfg.width = 2                 // a video stream is required; keep it tiny and slow
    cfg.height = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    let s = SCStream(filter: SCContentFilter(display: display, excludingWindows: []), configuration: cfg, delegate: self)
    try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)   // frames are ignored
    try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
    try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
    try await s.startCapture()
    stream = s
  }

  func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
    guard let track = tracks[type], sb.isValid, let desc = sb.formatDescription else { return }
    let format = AVAudioFormat(cmAudioFormatDescription: desc)
    let frames = AVAudioFrameCount(sb.numSamples)
    guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
    buf.frameLength = frames
    guard CMSampleBufferCopyPCMDataIntoAudioBufferList(
      sb, at: 0, frameCount: Int32(frames), into: buf.mutableAudioBufferList) == noErr else { return }
    try? track.append(buf, at: track.position(sb.presentationTimeStamp))
  }

  // Capture died (permission revoked, display gone…): keep what was recorded.
  func stream(_ stream: SCStream, didStopWithError error: Error) {
    FileHandle.standardError.write("capture stopped: \(error.localizedDescription)\n".data(using: .utf8)!)
    finish(code: 1)
  }

  func stop() {
    Task {
      try? await stream?.stopCapture()
      finish(code: 0)
    }
  }

  func finish(code: Int32) {
    queue.sync {
      if stopping { return }
      stopping = true
      tracks.removeAll()   // releasing each AVAudioFile finalises the .m4a
    }
    exit(code)
  }
}

let rec = Recorder()

// Stop cleanly on Ctrl-C / SIGTERM, and when the app that started us exits (even on a
// crash), so a recording is never left running in the background.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let stopSources: [DispatchSourceProtocol] = [
  DispatchSource.makeSignalSource(signal: SIGINT, queue: .main),
  DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main),
  DispatchSource.makeProcessSource(identifier: getppid(), eventMask: .exit, queue: .main),
]
for s in stopSources {
  s.setEventHandler { rec.stop() }
  s.resume()
}

Task {
  do {
    try await rec.start(dir: URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "."))
    print("recording")
    fflush(stdout)
  } catch {
    FileHandle.standardError.write("\(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
  }
}
dispatchMain()
