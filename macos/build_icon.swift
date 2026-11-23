import AppKit
import CoreGraphics
import Foundation

func renderSquircleIcon(
    sourceCG: CGImage,
    size: Int,
    fullBleed: Bool
) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: size,
        pixelsHigh: size,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )!

    NSGraphicsContext.saveGraphicsState()
    let gctx = NSGraphicsContext(bitmapImageRep: rep)!
    gctx.shouldAntialias = true
    gctx.imageInterpolation = .high
    NSGraphicsContext.current = gctx
    let cg = gctx.cgContext
    cg.interpolationQuality = .high

    let s = CGFloat(size)
    // Standard Apple macOS app icon grid: 824 / 1024 body (~9.77% padding per side)
    let pad: CGFloat = fullBleed ? 0.0 : round(s * 0.09765625)
    let rect = NSRect(x: pad, y: pad, width: s - pad * 2.0, height: s - pad * 2.0)
    let radius = rect.width * 0.224
    let squircle = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)

    // Soft macOS drop shadow for standard Dock icon sizes
    if !fullBleed && size >= 32 {
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowOffset = NSSize(width: 0, height: -max(1.0, s * 0.01))
        shadow.shadowBlurRadius = max(2.0, s * 0.024)
        shadow.shadowColor = NSColor(calibratedWhite: 0.0, alpha: 0.22)
        shadow.set()
        NSColor.white.setFill()
        squircle.fill()
        NSGraphicsContext.restoreGraphicsState()
    } else {
        NSColor.white.setFill()
        squircle.fill()
    }

    // Clip artwork inside squircle
    NSGraphicsContext.saveGraphicsState()
    squircle.addClip()
    NSColor.white.setFill()
    rect.fill()

    // Slight zoom for small web/favicon full-bleed icon so symbol stays bold at 16–28px
    let artInset: CGFloat = fullBleed ? -rect.width * 0.035 : 0.0
    let artRect = rect.insetBy(dx: artInset, dy: artInset)
    cg.draw(sourceCG, in: artRect)

    // Subtle crisp inner hairline border so white squircle remains defined on light surfaces
    let borderInset = max(0.5, s * 0.004)
    let strokePath = NSBezierPath(
        roundedRect: rect.insetBy(dx: borderInset, dy: borderInset),
        xRadius: max(1.0, radius - borderInset),
        yRadius: max(1.0, radius - borderInset)
    )
    strokePath.lineWidth = max(1.0, s * 0.008)
    NSColor(calibratedWhite: 0.0, alpha: fullBleed ? 0.08 : 0.06).setStroke()
    strokePath.stroke()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("Usage: build_icon <master_png> <iconset_out_dir> [web_logo_out_png]\n", stderr)
    exit(1)
}

let masterPath = args[1]
let iconsetDir = URL(fileURLWithPath: args[2])

guard let srcImg = NSImage(contentsOfFile: masterPath),
      let tiff = srcImg.tiffRepresentation,
      let srcRep = NSBitmapImageRep(data: tiff),
      let srcCG = srcRep.cgImage else {
    fputs("Failed to load master image at \(masterPath)\n", stderr)
    exit(1)
}

try FileManager.default.createDirectory(at: iconsetDir, withIntermediateDirectories: true)

let specs: [(Int, String)] = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]

for (px, name) in specs {
    let rep = renderSquircleIcon(sourceCG: srcCG, size: px, fullBleed: false)
    if let png = rep.representation(using: .png, properties: [:]) {
        try png.write(to: iconsetDir.appendingPathComponent(name))
    }
}

if args.count >= 4 {
    let webOutURL = URL(fileURLWithPath: args[3])
    try FileManager.default.createDirectory(
        at: webOutURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let webRep = renderSquircleIcon(sourceCG: srcCG, size: 256, fullBleed: true)
    if let png = webRep.representation(using: .png, properties: [:]) {
        try png.write(to: webOutURL)
    }
}
