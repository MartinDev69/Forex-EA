import 'package:flutter/material.dart';
import '../api/client.dart';
import 'broker.dart';
import 'dashboard.dart';
import 'strategies.dart';
import 'trades.dart';
import 'users.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({
    super.key,
    required this.apiClient,
    required this.onSignedOut,
    required this.onForgetDevice,
  });
  final ApiClient apiClient;
  final VoidCallback onSignedOut;
  final VoidCallback onForgetDevice;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final isAdmin = widget.apiClient.isAdmin;
    // Non-admin users only see Dashboard + Broker. Strategies, Trades,
    // Users all reflect the singleton bot's MT5 connection (not theirs)
    // so showing them would be misleading.
    final pages = <Widget>[
      DashboardScreen(
        apiClient: widget.apiClient,
        onSignedOut: widget.onSignedOut,
        onForgetDevice: widget.onForgetDevice,
      ),
      BrokerScreen(apiClient: widget.apiClient),
      if (isAdmin) StrategiesScreen(apiClient: widget.apiClient),
      if (isAdmin) TradesScreen(apiClient: widget.apiClient),
      if (isAdmin) UsersScreen(apiClient: widget.apiClient),
    ];
    final destinations = <NavigationDestination>[
      const NavigationDestination(
        icon: Icon(Icons.dashboard_outlined),
        selectedIcon: Icon(Icons.dashboard),
        label: 'Dashboard',
      ),
      const NavigationDestination(
        icon: Icon(Icons.account_balance_outlined),
        selectedIcon: Icon(Icons.account_balance),
        label: 'Broker',
      ),
      if (isAdmin)
        const NavigationDestination(
          icon: Icon(Icons.tune_outlined),
          selectedIcon: Icon(Icons.tune),
          label: 'Strategies',
        ),
      if (isAdmin)
        const NavigationDestination(
          icon: Icon(Icons.history_outlined),
          selectedIcon: Icon(Icons.history),
          label: 'Trades',
        ),
      if (isAdmin)
        const NavigationDestination(
          icon: Icon(Icons.group_outlined),
          selectedIcon: Icon(Icons.group),
          label: 'Users',
        ),
    ];
    // If role changes (e.g. self-demote) and selected index falls off the end.
    final safeIndex = _index.clamp(0, pages.length - 1);
    return Scaffold(
      body: IndexedStack(index: safeIndex, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: safeIndex,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: destinations,
      ),
    );
  }
}
