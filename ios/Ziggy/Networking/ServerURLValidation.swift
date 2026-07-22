import Foundation

enum ZiggyServerURLValidation {
    static let productionHost = "chat.mihaichiorean.com"
    static let productionURL = URL(string: "https://\(productionHost)")!

    static func isValid(_ url: URL) -> Bool {
        isProduction(url) || isLocalDevelopment(url)
    }

    static func isTrustedForIdentityToken(_ url: URL) -> Bool {
        isValid(url)
    }

    static func isProduction(_ url: URL) -> Bool {
        guard let components = sanitizedComponents(for: url),
              components.scheme?.lowercased() == "https",
              components.host?.lowercased() == productionHost else {
            return false
        }
        return components.port == nil || components.port == 443
    }

    static func isLocalDevelopment(_ url: URL) -> Bool {
        guard let components = sanitizedComponents(for: url),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = components.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")) else {
            return false
        }
        return ["localhost", "127.0.0.1", "::1"].contains(host)
    }

    static func url(from value: String) -> URL? {
        guard let url = URL(string: value), isValid(url) else { return nil }
        return url
    }

    private static func sanitizedComponents(for url: URL) -> URLComponents? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = components.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")),
              !host.isEmpty,
              components.user == nil,
              components.password == nil,
              components.percentEncodedQuery == nil,
              components.percentEncodedFragment == nil else {
            return nil
        }

        return components
    }
}
