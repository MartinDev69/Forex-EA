class EaConfig {
  EaConfig({
    required this.apiBaseUrl,
    required this.apiKey,
    required this.adId,
  });

  final String apiBaseUrl;
  final String apiKey;
  final String adId;

  factory EaConfig.fromJson(Map<String, dynamic> json) {
    return EaConfig(
      apiBaseUrl: json['api_base_url'] as String? ?? '',
      apiKey: json['api_key'] as String? ?? '',
      adId: json['ad_id'] as String? ?? '',
    );
  }
}
