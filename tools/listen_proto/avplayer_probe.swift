// AVPlayer probe for the phone-audio HLS stream (macOS; AVFoundation is the
// engine Safari uses for native HLS). Plays muted, prints once every 0.5 s:
// player status, how far the playing position's PROGRAM-DATE-TIME is ahead of
// now (positive = the phone would be ahead of the device), and the distance to
// the live edge. From 6 s on, every 4 s, if the error is over 30 ms it seeks
// precisely so that PDT == now, the way the listener page does, adding what the
// previous seek lost while it was buffering (learned, as the page would learn it).
//
//   swift tools/listen_proto/avplayer_probe.swift http://127.0.0.1:8199/listen/d0/live.m3u8 25
import AVFoundation
import Foundation

let url = URL(string: CommandLine.arguments[1])!
let seconds = CommandLine.arguments.count > 2 ? Double(CommandLine.arguments[2])! : 25
let player = AVPlayer(url: url)
player.isMuted = true
player.play()
let start = Date()
var nextCheck = 6.0
var compensation = 0.0
var lastSeekAhead: Double? = nil
var stalls = 0

_ = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
    guard let item = player.currentItem else { return }
    let elapsed = Date().timeIntervalSince(start)
    var line = String(format: "t=%5.1f status=%d rate=%.1f ct=%7.3f", elapsed,
                      item.status.rawValue, player.rate, item.currentTime().seconds)
    var ahead: Double? = nil
    if let date = item.currentDate() {
        ahead = date.timeIntervalSince(Date())
        line += String(format: " pdt_minus_now=%+.3f", ahead!)
    }
    if let range = item.seekableTimeRanges.last?.timeRangeValue {
        line += String(format: " edge=%.2f", (range.end - item.currentTime()).seconds)
    }
    if item.status == .failed { line += " error=\(String(describing: item.error))" }
    print(line)
    fflush(stdout)
    if player.timeControlStatus == .waitingToPlayAtSpecifiedRate { stalls += 1; print("STALL") }
    if elapsed > nextCheck, let ahead = ahead, player.rate > 0 {
        nextCheck = elapsed + 4
        if lastSeekAhead != nil {
            // What the last seek left over is what it cost; learn it.
            compensation -= ahead
            lastSeekAhead = nil
        }
        if abs(ahead) > 0.03 {
            let shift = -ahead + compensation
            let target = item.currentTime() + CMTime(seconds: shift, preferredTimescale: 16000)
            lastSeekAhead = ahead
            item.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { done in
                print(String(format: "SEEK by %+.3f s (compensation %+.3f) done=%d", shift, compensation, done ? 1 : 0))
            }
        }
    }
    if elapsed > seconds { exit(0) }
}
RunLoop.main.run()
