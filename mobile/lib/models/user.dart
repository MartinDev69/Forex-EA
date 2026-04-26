class AppUser {
  AppUser({
    required this.username,
    required this.role,
    required this.email,
    required this.createdAt,
    required this.passwordSet,
  });

  final String username;
  final String role;
  final String? email;
  final String createdAt;
  final bool passwordSet;

  bool get isAdmin => role == 'admin';
  bool get isPending => !passwordSet && role != 'admin';

  factory AppUser.fromJson(Map<String, dynamic> j) => AppUser(
        username: j['username'] as String,
        role: j['role'] as String,
        email: j['email'] as String?,
        createdAt: (j['created_at'] as String?) ?? '',
        passwordSet: (j['password_set'] as bool?) ?? true,
      );
}

class AdIdPool {
  AdIdPool({required this.unclaimed, required this.size});

  final List<String> unclaimed;
  final int size;

  factory AdIdPool.fromJson(Map<String, dynamic> j) => AdIdPool(
        unclaimed: (j['unclaimed'] as List<dynamic>).cast<String>(),
        size: j['size'] as int,
      );
}

class AssignResult {
  AssignResult({
    required this.adId,
    required this.email,
    required this.expiresAt,
    this.setupUrl,
  });

  final String adId;
  final String email;
  final int expiresAt;
  final String? setupUrl;

  factory AssignResult.fromJson(Map<String, dynamic> j) => AssignResult(
        adId: j['ad_id'] as String,
        email: j['email'] as String,
        expiresAt: j['setup_expires_at'] as int,
        setupUrl: j['setup_url'] as String?,
      );
}
