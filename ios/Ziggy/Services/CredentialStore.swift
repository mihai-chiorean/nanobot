import Foundation

enum CredentialKey: Hashable, Sendable {
    case serverURL

    var account: String {
        "server-url"
    }

    var displayName: String {
        "server URL"
    }
}

enum CredentialStoreOperation: String, Sendable {
    case read
    case save
    case delete
}

enum CredentialValueValidationReason: Sendable, Equatable {
    case empty
    case invalidServerURL
    case leadingOrTrailingWhitespace
}

enum CredentialStoreError: Error, Equatable, LocalizedError, Sendable {
    case invalidValue(key: CredentialKey, reason: CredentialValueValidationReason)
    case invalidStoredValue(key: CredentialKey)
    case unexpectedKeychainData(key: CredentialKey)
    case keychainFailure(operation: CredentialStoreOperation, key: CredentialKey, status: Int32)

    var errorDescription: String? {
        switch self {
        case let .invalidValue(key, reason):
            switch reason {
            case .empty:
                "The \(key.displayName) cannot be empty."
            case .invalidServerURL:
                "The server URL must use HTTPS, or HTTP on localhost, 127.0.0.1, or ::1, with no user info, query, or fragment."
            case .leadingOrTrailingWhitespace:
                "The \(key.displayName) contains leading or trailing whitespace."
            }
        case let .invalidStoredValue(key):
            "The saved \(key.displayName) is invalid."
        case let .unexpectedKeychainData(key):
            "The saved \(key.displayName) could not be decoded from Keychain data."
        case let .keychainFailure(operation, key, status):
            "Keychain could not \(operation.rawValue) the \(key.displayName) (OSStatus \(status))."
        }
    }

    var recoverySuggestion: String? {
        switch self {
        case .invalidValue:
            "Check the value and try again."
        case .invalidStoredValue, .unexpectedKeychainData:
            "Remove the saved credential and enroll again."
        case .keychainFailure:
            "Retry the operation. If it continues, check device Keychain access and restart Ziggy."
        }
    }
}

protocol CredentialStoring: Sendable {
    func value(for key: CredentialKey) async throws -> String?
    func setValue(_ value: String, for key: CredentialKey) async throws
    func removeValue(for key: CredentialKey) async throws
}

extension CredentialStoring {
    func serverURL() async throws -> URL? {
        guard let value = try await value(for: .serverURL) else {
            return nil
        }

        guard let url = ZiggyServerURLValidation.url(from: value) else {
            throw CredentialStoreError.invalidStoredValue(key: .serverURL)
        }
        return url
    }

    func save(serverURL: URL) async throws {
        guard ZiggyServerURLValidation.isValid(serverURL) else {
            throw CredentialStoreError.invalidValue(key: .serverURL, reason: .invalidServerURL)
        }
        try await setValue(serverURL.absoluteString, for: .serverURL)
    }

    func removeServerURL() async throws {
        try await removeValue(for: .serverURL)
    }
}

enum CredentialStoreValidation {
    static func validate(_ value: String, for key: CredentialKey) throws {
        guard !value.isEmpty else {
            throw CredentialStoreError.invalidValue(key: key, reason: .empty)
        }
        guard value == value.trimmingCharacters(in: .whitespacesAndNewlines) else {
            throw CredentialStoreError.invalidValue(key: key, reason: .leadingOrTrailingWhitespace)
        }

        guard ZiggyServerURLValidation.url(from: value) != nil else {
            throw CredentialStoreError.invalidValue(key: key, reason: .invalidServerURL)
        }
    }
}

actor InMemoryCredentialStore: CredentialStoring {
    private var values: [CredentialKey: String] = [:]

    func value(for key: CredentialKey) async throws -> String? {
        values[key]
    }

    func setValue(_ value: String, for key: CredentialKey) async throws {
        try CredentialStoreValidation.validate(value, for: key)
        values[key] = value
    }

    func removeValue(for key: CredentialKey) async throws {
        values[key] = nil
    }
}
