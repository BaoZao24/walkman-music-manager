import AppKit
import Foundation

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: make_icon.swift <output.png>\n", stderr)
    exit(2)
}

let size = 1024
let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let bitmap = NSBitmapImageRep(
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
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
context.imageInterpolation = .high

let canvas = NSRect(x: 0, y: 0, width: size, height: size)
NSColor(calibratedRed: 0.055, green: 0.06, blue: 0.065, alpha: 1).setFill()
NSBezierPath(roundedRect: canvas.insetBy(dx: 40, dy: 40), xRadius: 215, yRadius: 215).fill()

let yellow = NSColor(calibratedRed: 1, green: 0.83, blue: 0.12, alpha: 1)
let white = NSColor(calibratedWhite: 0.94, alpha: 1)
let device = NSBezierPath(roundedRect: NSRect(x: 245, y: 126, width: 534, height: 772), xRadius: 84, yRadius: 84)
NSColor(calibratedWhite: 0.075, alpha: 1).setFill()
device.fill()
yellow.setStroke()
device.lineWidth = 18
device.stroke()

white.setStroke()
let speaker = NSBezierPath(roundedRect: NSRect(x: 390, y: 782, width: 244, height: 16), xRadius: 8, yRadius: 8)
speaker.lineWidth = 1
speaker.stroke()

let play = NSBezierPath()
play.move(to: NSPoint(x: 430, y: 365))
play.line(to: NSPoint(x: 430, y: 659))
play.line(to: NSPoint(x: 666, y: 512))
play.close()
yellow.setFill()
play.fill()

NSColor(calibratedWhite: 0.5, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 390, y: 228, width: 115, height: 15), xRadius: 7, yRadius: 7).fill()
yellow.setFill()
NSBezierPath(roundedRect: NSRect(x: 518, y: 228, width: 116, height: 15), xRadius: 7, yRadius: 7).fill()

NSGraphicsContext.restoreGraphicsState()
guard let png = bitmap.representation(using: .png, properties: [:]) else {
    fputs("Could not encode icon PNG.\n", stderr)
    exit(1)
}
try png.write(to: outputURL)
