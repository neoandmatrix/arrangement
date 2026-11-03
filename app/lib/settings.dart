import 'package:shared_preferences/shared_preferences.dart';

/// What this phone remembers: which server, the access key, and who is using it.
class Settings {
  static late SharedPreferences _prefs;

  static Future<void> load() async => _prefs = await SharedPreferences.getInstance();

  /// e.g. http://192.168.1.20:8765 on the same Wi-Fi, or https://xyz.trycloudflare.com
  static String get server => _prefs.getString('server') ?? '';
  static Future<void> setServer(String v) => _prefs.setString('server', v.trim().replaceAll(RegExp(r'/+$'), ''));

  static String get appKey => _prefs.getString('app_key') ?? '';
  static Future<void> setAppKey(String v) => _prefs.setString('app_key', v.trim());

  /// 'family' or 'vendor'; null until chosen.
  static String? get role => _prefs.getString('role');
  static Future<void> setRole(String? v) => v == null ? _prefs.remove('role') : _prefs.setString('role', v);

  static String? get vendorId => _prefs.getString('vendor_id');
  static String? get vendorName => _prefs.getString('vendor_name');
  static Future<void> setVendor(String id, String name) async {
    await _prefs.setString('vendor_id', id);
    await _prefs.setString('vendor_name', name);
  }

  static bool get configured => server.isNotEmpty && role != null && (role == 'family' || vendorId != null);
}
