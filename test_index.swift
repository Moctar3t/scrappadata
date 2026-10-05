import Foundation

struct IndexEntry: Decodable {
    let id: String
    let title: String
    let artist: String
    let coverArt: String?
    let duration: String?
    let bpm: Int?
    let key: String?
    let producers: String?
    let writers: String?
    let hasStems: Bool?
    
    let genres: [String]?
    let danceability: Double?
    let energy: Double?
    let acousticness: Double?
    let instrumentalness: Double?
    let valence: Double?
    let speechiness: Double?
    let popularity: Int?
}

struct IndexFile: Decodable {
    let songsList: [IndexEntry]
    let stemFormat: String?
    let featured: String?
}

let data = try Data(contentsOf: URL(fileURLWithPath: "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json"))
do {
    let decoded = try JSONDecoder().decode(IndexFile.self, from: data)
    print("SUCCESS: Decoded index with \(decoded.songsList.count) songs")
} catch {
    print("FAILED: \(error)")
}
