// 用 macOS Vision 识别图片里的文字，供 xiaoqu.py 调用
//   ocr <图片>                               整张图（长图切段识别）
//   ocr <图片> <y0> <y1> <x0> <x1> <倍数>     只识别这块区域：放大并去掉浅色（压在字上的浅蓝水印），用于补识别被水印盖住的小区名
// 输出：第一行 "W\tH"（原图像素宽高），之后每段文字一行 "y\tx\th\ttext"（中心点坐标、高度，原点在左上角，单位为原图像素）
// 编译：swiftc -O ocr.swift -o .ocr_bin（xiaoqu.py 会在需要时自动编译）
import AppKit
import Vision

let args = CommandLine.arguments
guard args.count > 1,
      let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
  FileHandle.standardError.write("usage: ocr <image> [y0 y1 x0 x1 scale]\n".data(using: .utf8)!)
  exit(1)
}
let W = Double(cg.width), H = Double(cg.height)
print("\(Int(W))\t\(Int(H))")

func recognize(_ image: CGImage) -> [VNRecognizedTextObservation] {
  let req = VNRecognizeTextRequest()
  req.recognitionLevel = .accurate
  req.recognitionLanguages = ["zh-Hans", "en-US"]
  req.usesLanguageCorrection = false
  do { try VNImageRequestHandler(cgImage: image).perform([req]) } catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(2)
  }
  return req.results ?? []
}
func emit(_ y: Double, _ x: Double, _ h: Double, _ t: String) {
  print(String(format: "%.1f\t%.1f\t%.1f\t%@", y, x, h, t.replacingOccurrences(of: "\t", with: " ")))
}

if args.count >= 7, let y0 = Int(args[2]), let y1 = Int(args[3]), let x0 = Int(args[4]), let x1 = Int(args[5]), let k = Double(args[6]) {
  // 区域模式：裁剪 → 放大 → 灰度 → 浅于阈值的像素变白
  let rect = CGRect(x: max(0, x0), y: max(0, y0), width: min(cg.width, x1) - max(0, x0), height: min(cg.height, y1) - max(0, y0))
  guard let part = cg.cropping(to: rect) else { exit(3) }
  let w = Int(rect.width * k), h = Int(rect.height * k)
  guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) else { exit(3) }
  ctx.interpolationQuality = .high
  ctx.draw(part, in: CGRect(x: 0, y: 0, width: w, height: h))
  let px = ctx.data!.bindMemory(to: UInt8.self, capacity: w * h)
  for i in 0..<(w * h) { px[i] = px[i] < 150 ? 0 : 255 }
  guard let clean = ctx.makeImage() else { exit(3) }
  for o in recognize(clean) {
    guard let t = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    emit(rect.minY + (1 - b.midY) * rect.height, rect.minX + b.midX * rect.width, b.height * rect.height, t.string)
  }
  exit(0)
}

// 整张图：长图整张识别时会被缩小，小字丢失；切成约 1000 像素高的段分别识别，段之间重叠 150 像素，
// 每段只保留中心点落在本段「核心区」的文字，避免重复
let tile = 1000, overlap = 150
var top = 0
while top < cg.height {
  let y0 = max(0, top - overlap), y1 = min(cg.height, top + tile + overlap)
  guard let part = cg.cropping(to: CGRect(x: 0, y: y0, width: cg.width, height: y1 - y0)) else { break }
  let ph = Double(y1 - y0)
  for o in recognize(part) {
    guard let t = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    let y = Double(y0) + (1 - b.midY) * ph
    if y < Double(top) || y >= Double(min(cg.height, top + tile)) { continue }
    emit(y, b.midX * W, b.height * ph, t.string)
  }
  top += tile
}
