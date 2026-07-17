import Foundation

enum EnrollmentCredentialMode: String, Sendable, CaseIterable {
    case ownerAccessCode = "owner-access-code"
    case guestCode = "guest-code"

    var displayName: String {
        switch self {
        case .ownerAccessCode:
            "owner access code"
        case .guestCode:
            "guest code"
        }
    }
}

enum CredentialKey: Hashable, Sendable {
    case serverURL
    case enrollment(EnrollmentCredentialMode)

    var account: String {
        switch self {
        case .serverURL:
            "server-url"
        case let .enrollment(mode):
            "enrollment-\(mode.rawValue)"
        }
    }

    var displayName: String {
        switch self {
        case .serverURL:
            "server URL"
        case let .enrollment(mode):
            mode.displayName
        }
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
    case ephemeralToken
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
                "The server URL must use http or https and include a host."
            case .leadingOrTrailingWhitespace:
                "The \(key.displayName) contains leading or trailing whitespace."
            case .ephemeralToken:
                "Short-lived nbwt_ tokens are kept in memory and cannot be saved."
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

        guard let url = URL(string: value), Self.isValidServerURL(url) else {
            throw CredentialStoreError.invalidStoredValue(key: .serverURL)
        }
        return url
    }

    func save(serverURL: URL) async throws {
        guard Self.isValidServerURL(serverURL) else {
            throw CredentialStoreError.invalidValue(key: .serverURL, reason: .invalidServerURL)
        }
        try await setValue(serverURL.absoluteString, for: .serverURL)
    }

    func enrollmentCredential(for mode: EnrollmentCredentialMode) async throws -> String? {
        try await value(for: .enrollment(mode))
    }

    func saveEnrollmentCredential(_ credential: String, for mode: EnrollmentCredentialMode) async throws {
        try await setValue(credential, for: .enrollment(mode))
    }

    func removeEnrollmentCredential(for mode: EnrollmentCredentialMode) async throws {
        try await removeValue(for: .enrollment(mode))
    }

    func guestEnrollmentCode() async throws -> String? {
        try await enrollmentCredential(for: .guestCode)
    }

    func ownerAccessCode() async throws -> String? {
        try await enrollmentCredential(for: .ownerAccessCode)
    }

    func save(guestEnrollmentCode: String) async throws {
        try await saveEnrollmentCredential(guestEnrollmentCode, for: .guestCode)
    }

    func save(ownerAccessCode: String) async throws {
        try await saveEnrollmentCredential(ownerAccessCode, for: .ownerAccessCode)
    }

    func removeServerURL() async throws {
        try await removeValue(for: .serverURL)
    }

    func removeGuestEnrollmentCode() async throws {
        try await removeEnrollmentCredential(for: .guestCode)
    }

    func removeOwnerAccessCode() async throws {
        try await removeEnrollmentCredential(for: .ownerAccessCode)
    }

    private static func isValidServerURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host,
              !host.isEmpty else {
            return false
        }
        return true
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

        if case .enrollment = key, value.hasPrefix("nbwt_") {
            throw CredentialStoreError.invalidValue(key: key, reason: .ephemeralToken)
        }

        if case .serverURL = key {
            guard let url = URL(string: value),
                  let scheme = url.scheme?.lowercased(),
                  (scheme == "http" || scheme == "https"),
                  url.host?.isEmpty == false else {
                throw CredentialStoreError.invalidValue(key: key, reason: .invalidServerURL)
            }
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
