import 'package:flutter/material.dart';
import '../api/client.dart';
import 'dashboard.dart';
import 'strategies.dart';
import 'trades.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final pages = [
      DashboardScreen(apiClient: widget.apiClient),
      StrategiesScreen(apiClient: widget.apiClient),
      TradesScreen(apiClient: widget.apiClient),
    ];
    return Scaffold(
      body: IndexedStack(index: _index, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: 'Dashboard',
          ),
          NavigationDestination(
            icon: Icon(Icons.tune_outlined),
            selectedIcon: Icon(Icons.tune),
            label: 'Strategies',
          ),
          NavigationDestination(
            icon: Icon(Icons.history_outlined),
            selectedIcon: Icon(Icons.history),
            label: 'Trades',
          ),
        ],
      ),
    );
  }
}
