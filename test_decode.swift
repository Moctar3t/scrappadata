import Foundation

struct SongStem: Decodable {
    let id: String
    let url: String
    enum CodingKeys: String, CodingKey { case id, url }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try c.decode(String.self, forKey: .id)
        if let single = try? c.decode(String.self, forKey: .url) {
            self.url = single
        } else if let many = try? c.decode([String].self, forKey: .url), let first = many.first {
            self.url = first
        } else {
            throw DecodingError.dataCorruptedError(forKey: .url, in: c, debugDescription: "bad url")
        }
    }
}
struct SongFile: Decodable {
    let stems: [SongStem]
    let producers: String?
    let writers: String?
    let recordLabel: String?
    let year: String?
    let releaseFormat: String?
}
let data = try Data(contentsOf: URL(fileURLWithPath: "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/songs/0002.json"))
do {
    let sf = try JSONDecoder().decode(SongFile.self, from: data)
    print("SUCCESS: decoded \(sf.stems.count) stems")
} catch {
    print("FAILED: \(error)")
}
