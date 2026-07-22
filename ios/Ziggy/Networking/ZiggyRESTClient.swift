import Foundation

public struct ZiggyRESTClient: Sendable {
    public let baseURL: URL
    public let session: URLSession
    public let maxResponseBytes: Int
    private let bearerToken: String?

    public init(baseURL: URL, bearerToken: String? = nil, session: URLSession = .shared,
                maxResponseBytes: Int = ZiggyProtocolLimits.maxRESTResponseBytes) {
        self.baseURL = baseURL
        self.bearerToken = bearerToken
        self.session = session
        self.maxResponseBytes = max(1, maxResponseBytes)
    }

    public func withBearerToken(_ token: String?) -> ZiggyRESTClient {
        ZiggyRESTClient(baseURL: baseURL, bearerToken: token, session: session,
                        maxResponseBytes: maxResponseBytes)
    }

    public func bootstrapAuthenticated(identityToken: String) async throws -> BootstrapResponse {
        try await request(path: ["auth", "bootstrap"], token: identityToken)
    }

    public func fetchSessions() async throws -> RESTListResponse<SessionSummary> {
        try await list(path: ["api", "sessions"])
    }

    public func fetchMessages(sessionKey: String) async throws -> RESTListResponse<ZiggyMessage> {
        try await list(path: ["api", "sessions", sessionKey, "messages"])
    }

    public func fetchWork() async throws -> RESTListResponse<WorkTask> {
        try await list(path: ["api", "work"])
    }

    public func fetchWorkTask(taskID: String) async throws -> WorkTask {
        let data = try await data(path: ["api", "work", taskID])
        let decoder = JSONDecoder.ziggy
        if let direct = try? decoder.decode(WorkTask.self, from: data) { return direct }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let task = object["task"] {
            return try decoder.decode(WorkTask.self, from: JSONSerialization.data(withJSONObject: task))
        }
        throw ZiggyRESTError.decoding
    }

    public func fetchWorkEvents(taskID: String, afterSequence: Int? = nil) async throws -> RESTListResponse<WorkEvent> {
        let query = afterSequence.map { [URLQueryItem(name: "after_seq", value: String($0))] } ?? []
        return try await list(path: ["api", "work", taskID, "events"], query: query)
    }

    public func fetchSettings() async throws -> SettingsSnapshot {
        try await request(path: ["api", "settings"])
    }

    private func list<Value: Codable & Sendable>(path: [String], query: [URLQueryItem] = []) async throws -> RESTListResponse<Value> {
        let data = try await data(path: path, query: query)
        return try decodeList(Value.self, from: data)
    }

    private func request<Value: Codable & Sendable>(path: [String], query: [URLQueryItem] = [],
                                                    headers: [String: String] = [:], token: String? = nil,
                                                    method: String = "GET", body: Data? = nil) async throws -> Value {
        let data = try await data(path: path, query: query, headers: headers, token: token,
                                  method: method, body: body)
        let decoder = JSONDecoder.ziggy
        if let direct = try? decoder.decode(Value.self, from: data) { return direct }
        if let envelope = try? decoder.decode(RESTEnvelope<Value>.self, from: data), let value = envelope.value { return value }
        throw ZiggyRESTError.decoding
    }

    private func data(path: [String], query: [URLQueryItem] = [], headers: [String: String] = [:],
                      token: String? = nil, method: String = "GET", body: Data? = nil) async throws -> Data {
        guard ZiggyServerURLValidation.isValid(baseURL) else { throw ZiggyRESTError.invalidURL }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let basePath = components?.percentEncodedPath ?? baseURL.path
        let encodedPath = path.map(Self.encodePathSegment).joined(separator: "/")
        components?.percentEncodedPath = basePath.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/" + encodedPath
        components?.queryItems = query.isEmpty ? nil : query
        guard let url = components?.url else { throw ZiggyRESTError.invalidURL }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        for (header, value) in headers { request.setValue(value, forHTTPHeaderField: header) }
        if let token = token ?? bearerToken { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (responseData, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ZiggyRESTError.invalidResponse }
        guard response.expectedContentLength <= 0 || response.expectedContentLength <= Int64(maxResponseBytes),
              responseData.count <= maxResponseBytes else {
            throw ZiggyRESTError.responseTooLarge(limit: maxResponseBytes)
        }
        guard (200..<300).contains(response.statusCode) else {
            let boundedBody = String(data: responseData, encoding: .utf8)?
                .ziggyTruncatedUTF8(maxBytes: ZiggyProtocolLimits.maxUnsupportedPayloadBytes)
            throw ZiggyRESTError.http(statusCode: response.statusCode, body: boundedBody)
        }
        return responseData
    }

    private static func encodePathSegment(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? segment
    }

    private func decodeList<Value: Codable & Sendable>(_ type: Value.Type, from data: Data) throws -> RESTListResponse<Value> {
        let decoder = JSONDecoder.ziggy
        if let array = try? decoder.decode([Value].self, from: data) { return RESTListResponse(items: array) }
        if let envelope = try? decoder.decode(RESTEnvelope<[Value]>.self, from: data), let items = envelope.value {
            return RESTListResponse(items: items)
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw ZiggyRESTError.decoding }
        var itemsData: Data
        if let value = object["sessions"] ?? object["messages"] ?? object["work"] ?? object["tasks"] ?? object["events"] ?? object["items"] ?? object["data"] {
            itemsData = try JSONSerialization.data(withJSONObject: value)
        } else { throw ZiggyRESTError.decoding }
        if let nested = try? decoder.decode([Value].self, from: itemsData) {
            let nextCursor = object["next_cursor"] as? String ?? object["nextCursor"] as? String
            let hasMore = object["has_more"] as? Bool ?? object["hasMore"] as? Bool
            return RESTListResponse(items: nested, nextCursor: nextCursor, hasMore: hasMore)
        }
        if let nestedObject = try? JSONSerialization.jsonObject(with: itemsData) as? [String: Any],
           let nestedValue = nestedObject["items"] ?? nestedObject["sessions"] ?? nestedObject["messages"] ?? nestedObject["data"] {
            itemsData = try JSONSerialization.data(withJSONObject: nestedValue)
        }
        let items = try decoder.decode([Value].self, from: itemsData)
        let nextCursor = object["next_cursor"] as? String ?? object["nextCursor"] as? String
        let hasMore = object["has_more"] as? Bool ?? object["hasMore"] as? Bool
        return RESTListResponse(items: items, nextCursor: nextCursor, hasMore: hasMore)
    }
}

public enum ZiggyRESTError: Error, Equatable, Sendable {
    case invalidURL
    case invalidResponse
    case http(statusCode: Int, body: String?)
    case responseTooLarge(limit: Int)
    case decoding
}
