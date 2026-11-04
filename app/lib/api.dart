import 'dart:convert';

import 'package:http/http.dart' as http;

import 'settings.dart';

class ApiError implements Exception {
  ApiError(this.message);
  final String message;
  @override
  String toString() => message;
}

/// The Bandobast server's app API (/app/...). Every request carries the access key.
class Api {
  static http.Client client = http.Client();
  static Uri uri(String path, [Map<String, String>? query]) =>
      Uri.parse('${Settings.server}/app$path').replace(queryParameters: query);

  static Map<String, String> get _headers => {
    'X-App-Key': Settings.appKey, 'X-Participant-ID': Settings.participantId, 'Content-Type': 'application/json',
  };

  static Future<dynamic> get(String path, [Map<String, String>? query]) =>
      _send(() => client.get(uri(path, query), headers: _headers));

  static Future<dynamic> post(String path, [Object? body]) =>
      _send(() => client.post(uri(path), headers: _headers, body: jsonEncode(body ?? {})));

  static Future<dynamic> _send(Future<http.Response> Function() request) async {
    if (Settings.server.isEmpty) throw ApiError('Set the server address first');
    final http.Response response;
    try {
      response = await request().timeout(const Duration(seconds: 30));
    } catch (e) {
      throw ApiError('Can\'t reach the server: $e');
    }
    final text = utf8.decode(response.bodyBytes);
    final body = text.isEmpty ? null : jsonDecode(text);
    if (response.statusCode == 401) throw ApiError('Wrong app key (check Settings)');
    if (response.statusCode >= 400) {
      final detail = body is Map ? body['detail'] : null;
      throw ApiError(detail?.toString() ?? 'Server error ${response.statusCode}');
    }
    return body;
  }

  /// The vendor's live connection, on which calls ring.
  static Uri presenceSocket(String participantId) {
    final base = Uri.parse(Settings.server);
    return base.replace(
      scheme: base.scheme == 'https' ? 'wss' : 'ws',
      path: '/app/presence/ws',
      queryParameters: {'participant_id': participantId},
    );
  }

  static Uri audioSocket(String path) {
    final base = Uri.parse(Settings.server);
    final audio = Uri.parse(path);
    return base.replace(scheme: base.scheme == 'https' ? 'wss' : 'ws', path: audio.path, query: audio.query);
  }
}
