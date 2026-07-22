import Foundation
import Security

final class KeychainCredentialStore: CredentialStoring, Sendable {
    private let service: String

    init(service: String = "com.mihaichiorean.ziggy.credentials") {
        self.service = service
    }

    func value(for key: CredentialKey) async throws -> String? {
        var query = baseQuery(for: key)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status != errSecItemNotFound else {
            return nil
        }
        guard status == errSecSuccess else {
            throw CredentialStoreError.keychainFailure(operation: .read, key: key, status: status)
        }
        guard let data = result as? Data else {
            throw CredentialStoreError.unexpectedKeychainData(key: key)
        }
        guard let value = String(data: data, encoding: .utf8) else {
            throw CredentialStoreError.unexpectedKeychainData(key: key)
        }
        do {
            try CredentialStoreValidation.validate(value, for: key)
        } catch {
            throw CredentialStoreError.invalidStoredValue(key: key)
        }
        return value
    }

    func setValue(_ value: String, for key: CredentialKey) async throws {
        try CredentialStoreValidation.validate(value, for: key)
        guard let data = value.data(using: .utf8) else {
            throw CredentialStoreError.unexpectedKeychainData(key: key)
        }

        var item = baseQuery(for: key)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly

        var status = SecItemAdd(item as CFDictionary, nil)
        if status == errSecDuplicateItem {
            let update: [String: Any] = [
                kSecValueData as String: data,
                kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            ]
            status = SecItemUpdate(baseQuery(for: key) as CFDictionary, update as CFDictionary)
        }

        guard status == errSecSuccess else {
            throw CredentialStoreError.keychainFailure(operation: .save, key: key, status: status)
        }
    }

    func removeValue(for key: CredentialKey) async throws {
        let status = SecItemDelete(baseQuery(for: key) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.keychainFailure(operation: .delete, key: key, status: status)
        }
    }

    private func baseQuery(for key: CredentialKey) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key.account
        ]
    }
}
