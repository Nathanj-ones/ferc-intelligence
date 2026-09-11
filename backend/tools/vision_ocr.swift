import Foundation
import ImageIO
import Vision

struct OCRFailure: Error, CustomStringConvertible {
    let description: String
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: vision_ocr.swift IMAGE\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("cannot read input image\n", stderr)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
// Language correction can involve model/resource resolution outside the
// bounded page recognizer.  Disable it: filed values and units must come from
// the page pixels, not an inferred language-model completion.
request.usesLanguageCorrection = false

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
} catch {
    fputs("Vision text recognition failed: \(error)\n", stderr)
    exit(4)
}

let observations = (request.results ?? []).sorted {
    if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.005 {
        return $0.boundingBox.midY > $1.boundingBox.midY
    }
    return $0.boundingBox.minX < $1.boundingBox.minX
}

var rows: [[String: Any]] = []
for observation in observations {
    guard let candidate = observation.topCandidates(1).first else { continue }
    rows.append([
        "text": candidate.string,
        "confidence": Double(candidate.confidence),
        "x": Double(observation.boundingBox.minX),
        "y": Double(observation.boundingBox.minY),
        "width": Double(observation.boundingBox.width),
        "height": Double(observation.boundingBox.height),
    ])
}

guard !rows.isEmpty else {
    fputs("Vision returned no text observations\n", stderr)
    exit(5)
}

let payload: [String: Any] = [
    "schema": "ferc-macos-vision-ocr-v1",
    "recognition_level": "accurate",
    "language_correction": false,
    "rows": rows,
]

do {
    let data = try JSONSerialization.data(withJSONObject: payload,
                                          options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
} catch {
    fputs("cannot encode OCR JSON: \(error)\n", stderr)
    exit(6)
}
