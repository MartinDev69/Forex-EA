class SubscriptionRequest {
  SubscriptionRequest({
    required this.id,
    required this.telegramChatId,
    required this.duration,
    required this.email,
    required this.status,
    required this.createdAt,
    this.telegramUsername,
    this.telegramFirstName,
    this.phoneNumber,
    this.decidedAt,
    this.decidedBy,
    this.assignedAdId,
    this.rejectionReason,
  });

  final int id;
  final int telegramChatId;
  final String? telegramUsername;
  final String? telegramFirstName;
  final String duration;
  final String email;
  final String? phoneNumber;
  final String status; // 'pending' | 'approved' | 'rejected'
  final DateTime createdAt;
  final DateTime? decidedAt;
  final String? decidedBy;
  final String? assignedAdId;
  final String? rejectionReason;

  /// Human display: Telegram first name if present, else @username, else
  /// a short chat-id fallback.
  String get displayName {
    if (telegramFirstName != null && telegramFirstName!.isNotEmpty) {
      return telegramFirstName!;
    }
    if (telegramUsername != null && telegramUsername!.isNotEmpty) {
      return '@$telegramUsername';
    }
    return 'chat $telegramChatId';
  }

  factory SubscriptionRequest.fromJson(Map<String, dynamic> json) {
    return SubscriptionRequest(
      id: json['id'] as int,
      telegramChatId: json['telegram_chat_id'] as int,
      telegramUsername: json['telegram_username'] as String?,
      telegramFirstName: json['telegram_first_name'] as String?,
      duration: json['duration'] as String? ?? '1m',
      email: json['email'] as String? ?? '',
      phoneNumber: json['phone_number'] as String?,
      status: json['status'] as String? ?? 'pending',
      createdAt: DateTime.parse(json['created_at'] as String),
      decidedAt: json['decided_at'] != null
          ? DateTime.parse(json['decided_at'] as String)
          : null,
      decidedBy: json['decided_by'] as String?,
      assignedAdId: json['assigned_ad_id'] as String?,
      rejectionReason: json['rejection_reason'] as String?,
    );
  }
}
