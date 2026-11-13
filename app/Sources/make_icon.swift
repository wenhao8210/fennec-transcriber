// 生成 Fennec App 图标（渐变圆角 + 波形），输出 .iconset 目录供 iconutil 使用
import CoreGraphics
import Foundation
import ImageIO

func render(size s: Int) -> CGImage? {
    let f = CGFloat(s)
    guard let cs = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
    guard let ctx = CGContext(data: nil, width: s, height: s, bitsPerComponent: 8, bytesPerRow: 0,
                              space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }

    let pad = f * 0.05
    let rect = CGRect(x: pad, y: pad, width: f - pad * 2, height: f - pad * 2)
    let radius = f * 0.22
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil))
    ctx.clip()

    let colors = [
        CGColor(srgbRed: 0.09, green: 0.45, blue: 0.98, alpha: 1),
        CGColor(srgbRed: 0.42, green: 0.20, blue: 0.92, alpha: 1),
    ] as CFArray
    if let grad = CGGradient(colorsSpace: cs, colors: colors, locations: [0, 1]) {
        ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: f), end: CGPoint(x: f, y: 0), options: [])
    }

    let heights: [CGFloat] = [0.30, 0.50, 0.72, 1.0, 0.72, 0.48, 0.26]
    let count = CGFloat(heights.count)
    let barW = f * 0.072
    let gap = f * 0.036
    var x = (f - (count * barW + (count - 1) * gap)) / 2
    ctx.setFillColor(CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.95))
    for h in heights {
        let bh = f * 0.40 * h
        let y = (f - bh) / 2
        ctx.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: barW, height: bh),
                           cornerWidth: barW / 2, cornerHeight: barW / 2, transform: nil))
        ctx.fillPath()
        x += barW + gap
    }
    return ctx.makeImage()
}

func writePNG(_ image: CGImage, to url: URL) {
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else { return }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write(Data("usage: make_icon <iconset-dir>\n".utf8))
    exit(1)
}

let outDir = URL(fileURLWithPath: args[1])
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

let files: [(String, Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
for (name, size) in files {
    if let img = render(size: size) { writePNG(img, to: outDir.appendingPathComponent(name)) }
}
print("图标已生成: \(outDir.path)")
