import Cocoa

/// Write the paisty paperclip as a 1024 PNG. Same curves as the menu mark.
let side: CGFloat = 1024
let path = NSBezierPath()
path.lineWidth = 2.3
path.lineCapStyle = .round
path.lineJoinStyle = .round
path.move(to: NSPoint(x: 4.8, y: 1.4))
path.line(to: NSPoint(x: 4.8, y: 10.2))
path.curve(to: NSPoint(x: 15.4, y: 10.0),
           controlPoint1: NSPoint(x: 4.8, y: 16.4),
           controlPoint2: NSPoint(x: 16.2, y: 16.2))
path.curve(to: NSPoint(x: 8.6, y: 8.6),
           controlPoint1: NSPoint(x: 14.6, y: 4.6),
           controlPoint2: NSPoint(x: 8.4, y: 5.0))
path.curve(to: NSPoint(x: 11.6, y: 11.8),
           controlPoint1: NSPoint(x: 8.8, y: 11.2),
           controlPoint2: NSPoint(x: 10.2, y: 12.6))
let bounds = path.bounds.insetBy(dx: -1.4, dy: -1.4)
let fit = min(side / bounds.width, side / bounds.height) * 0.62
var t = AffineTransform()
t.translate(x: (side - bounds.width * fit) / 2 - bounds.minX * fit,
            y: (side - bounds.height * fit) / 2 - bounds.minY * fit)
t.scale(fit)
let weight = path.lineWidth
path.transform(using: t)
path.lineWidth = weight * fit

let image = NSImage(size: NSSize(width: side, height: side))
image.lockFocus()
NSColor(srgbRed: 0.953, green: 0.937, blue: 0.902, alpha: 1).setFill()
NSRect(x: 0, y: 0, width: side, height: side).fill()
NSColor.black.setStroke()
path.stroke()
image.unlockFocus()
guard let tiff = image.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fputs("png\n", stderr)
    exit(1)
}
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/paisty-icon.png"
try png.write(to: URL(fileURLWithPath: out))
