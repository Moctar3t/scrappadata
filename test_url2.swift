import Foundation
let urlStr = "https://dimesystemsstorage.b-cdn.net/Celeste%20-%20This%20Is%20Who%20I%20Am%20-%20From%20%E2%80%9CThe%20Day%20of%20the%20Jackal%E2%80%9D/bass.mp3"
let url = URL(string: urlStr)!
let newUrl = url.deletingPathExtension().appendingPathExtension("m4a")
print("New URL: \(newUrl.absoluteString)")
