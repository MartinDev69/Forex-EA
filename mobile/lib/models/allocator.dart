class Allocation {
  final String strategy;
  final String symbol;
  final String role; // champion | challenger | probe | cold
  final double weight;
  final int sampleSize;
  final double avgR;
  final double winRate;
  final String note;
  final String updatedAt;

  Allocation({
    required this.strategy,
    required this.symbol,
    required this.role,
    required this.weight,
    required this.sampleSize,
    required this.avgR,
    required this.winRate,
    required this.note,
    required this.updatedAt,
  });

  factory Allocation.fromJson(Map<String, dynamic> json) => Allocation(
        strategy: json['strategy'] as String,
        symbol: json['symbol'] as String,
        role: json['role'] as String,
        weight: (json['weight'] as num).toDouble(),
        sampleSize: json['sample_size'] as int,
        avgR: (json['avg_r'] as num).toDouble(),
        winRate: (json['win_rate'] as num).toDouble(),
        note: json['note'] as String,
        updatedAt: json['updated_at'] as String,
      );
}

class AllocatorResponse {
  final List<Allocation> allocations;
  final int count;

  AllocatorResponse({required this.allocations, required this.count});

  factory AllocatorResponse.fromJson(Map<String, dynamic> json) => AllocatorResponse(
        allocations: (json['allocations'] as List<dynamic>)
            .map((e) => Allocation.fromJson(e as Map<String, dynamic>))
            .toList(),
        count: json['count'] as int,
      );
}
