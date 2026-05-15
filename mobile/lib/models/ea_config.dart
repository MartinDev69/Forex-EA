class EaConfig {
  EaConfig({
    required this.apiBaseUrl,
    required this.apiKey,
    required this.apiKeySet,
    required this.adId,
  });

  final String apiBaseUrl;
  // Empty string when the key is set but hashed (plaintext not stored).
  // Use [apiKeySet] to distinguish "no key yet" from "key set but hidden".
  final String apiKey;
  final bool apiKeySet;
  final String adId;

  factory EaConfig.fromJson(Map<String, dynamic> json) {
    return EaConfig(
      apiBaseUrl: json['api_base_url'] as String? ?? '',
      apiKey: json['api_key'] as String? ?? '',
      apiKeySet: json['api_key_set'] as bool? ?? false,
      adId: json['ad_id'] as String? ?? '',
    );
  }
}
