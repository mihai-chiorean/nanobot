import ClerkKit
import Foundation

struct ZiggyIdentity: Sendable, Equatable {
    let name: String
    let email: String

    static let signedOut = ZiggyIdentity(name: "Ziggy user", email: "Not signed in")
}

@MainActor
protocol AuthSessionProviding: AnyObject {
    var isSignedIn: Bool { get }
    var identity: ZiggyIdentity? { get }

    func sessionToken() async throws -> String
    func signOut() async throws
}

enum AuthSessionError: Error, LocalizedError {
    case notConfigured
    case notSignedIn
    case missingToken

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            "Authentication is not configured."
        case .notSignedIn:
            "Sign in to continue."
        case .missingToken:
            "The identity provider did not return a session token."
        }
    }
}

@MainActor
final class ClerkAuthSession: AuthSessionProviding {
    private let clerk: Clerk

    init(clerk: Clerk) {
        self.clerk = clerk
    }

    var isSignedIn: Bool { clerk.session != nil }

    var identity: ZiggyIdentity? {
        guard let user = clerk.user,
              let email = user.primaryEmailAddress?.emailAddress else {
            return nil
        }
        let name = [user.firstName, user.lastName]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        return ZiggyIdentity(name: name.isEmpty ? email : name, email: email)
    }

    func sessionToken() async throws -> String {
        guard clerk.session != nil else { throw AuthSessionError.notSignedIn }
        guard let token = try await clerk.auth.getToken(), !token.isEmpty else {
            throw AuthSessionError.missingToken
        }
        return token
    }

    func signOut() async throws {
        try await clerk.auth.signOut()
    }
}

@MainActor
final class UnconfiguredAuthSession: AuthSessionProviding {
    var isSignedIn: Bool { false }
    var identity: ZiggyIdentity? { nil }

    func sessionToken() async throws -> String { throw AuthSessionError.notConfigured }
    func signOut() async throws {}
}
