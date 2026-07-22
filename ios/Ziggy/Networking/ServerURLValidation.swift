import Foundation

enum ZiggyServerURLValidation {
    static func isValid(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = components.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")),
              !host.isEmpty,
              components.user == nil,
              components.password == nil,
              components.percentEncodedQuery == nil,
              components.percentEncodedFragment == nil else {
            return false
        }

        if scheme == "https" {
            return true
        }

        return ["localhost", "127.0.0.1", "::1"].contains(host)
    }

    static func url(from value: String) -> URL? {
        guard let url = URL(string: value), isValid(url) else { return nil }
        return url
    }
}
