//+------------------------------------------------------------------+
//|                                            AntiGreedCopier.mq5  |
//|        Copies AntiGreed bot trades onto this MT5 account.       |
//+------------------------------------------------------------------+
//|                                                                  |
//|  Install:                                                        |
//|    1. Copy this file to <MT5 data folder>\MQL5\Experts\          |
//|    2. Open in MetaEditor, press F7 to compile.                   |
//|    3. Enable Algo Trading in MT5 (the green play button).        |
//|    4. Drag the EA onto ANY chart (the symbol doesn't matter —    |
//|       the EA places trades on whichever symbols the bot fires).  |
//|    5. Paste ApiBaseUrl, ApiToken, and AD-ID from your AntiGreed  |
//|       dashboard's "Your EA setup" panel.                         |
//|    6. Also add your ApiBaseUrl to:                               |
//|         Tools → Options → Expert Advisors → "Allow WebRequest    |
//|         for listed URL"                                          |
//|       — otherwise MT5 silently refuses outbound HTTP calls.      |
//|                                                                  |
//|  How it works:                                                   |
//|    Polls /signals/feed every PollSeconds. Each OPEN event opens  |
//|    a matching market order; each CLOSE event closes the          |
//|    corresponding position. Lot sizes are scaled by RiskMultiplier|
//|    (and clamped to MaxLotPerTrade). The mapping from the source  |
//|    bot's trade_id → the position ticket it opened on YOUR account|
//|    is held in memory and persisted to a chart-global variable so |
//|    a terminal restart doesn't lose track.                        |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Martin Kristof"
#property link      "https://github.com/MartinDev69/Forex-EA"
#property version   "1.08"
#property strict

// Build stamp shown on the panel's header sub-line — bumped on every
// layout change. If the panel doesn't show this exact string, MT5 is
// running a stale compiled binary; recompile and re-attach the EA.
#define EA_BUILD "v1.08"

#include <Trade/Trade.mqh>

input string  ApiBaseUrl        = "http://163.5.178.251:8000";  // from your dashboard
input string  ApiToken          = "";                            // ea_... key from dashboard
input string  AdId              = "";                            // your AD-ID (informational)
input int     PollSeconds       = 5;                             // how often to poll the feed
input double  RiskMultiplier    = 1.0;                           // scale every signal's lot
input double  MaxLotPerTrade    = 1.0;                           // hard cap
input string  SymbolSuffix      = "";                            // e.g. "m" if your broker uses EURUSDm
input string  EnabledSymbols    = "";                            // comma-separated whitelist (empty = all)
input bool    PlaceTakeProfit   = true;                          // mirror admin's TP
input bool    PlaceStopLoss     = true;                          // mirror admin's SL
input long    Magic             = 271828;                        // ours-vs-theirs filter
input bool    Verbose           = true;                          // chatty journal logging
input int     AccountReportSeconds = 60;                         // how often to push account snapshot

input group "═══ On-chart panel ═══"
input bool                ShowPanel         = true;              // render the status panel
input bool                PanelCentered     = true;              // auto-center on the chart (overrides offsets)
input int                 PanelWidth        = 720;               // panel width in px
input int                 PanelHeight       = 0;                 // 0 = auto-fit; otherwise px height
input int                 PanelOffsetX      = 0;                 // nudge X (px) — relative to centered position or top-left
input int                 PanelOffsetY      = 0;                 // nudge Y (px)
input color               PanelBgColor      = C'10,14,20';       // panel background
input color               PanelBorderColor  = C'34,238,136';     // brand neon green
input color               PanelTextColor    = C'232,240,255';    // body text
input color               PanelMutedColor   = C'130,148,176';    // labels
input color               PanelAccentColor  = C'34,238,136';     // numbers / accent
input color               PanelDangerColor  = C'255,84,116';     // stopped / errors
input string              PanelLogoFile     = "antigreed-logo.bmp"; // place in MQL5\Files

CTrade trade;

// Bookmark we send back as ?since= on the next poll. ISO-8601.
string g_bookmark = "";

// Wall clock of the last successful account snapshot POST.
datetime g_last_account_report = 0;

// Filter state — parsed once from EnabledSymbols input on init.
string   g_enabled_symbols[];
int      g_enabled_count    = 0;
bool     g_filter_active    = false;

// Panel state — counters/strings the redraw reads from.
int      g_copies_today     = 0;       // resets on local date roll-over
string   g_today_key        = "";      // YYYY-MM-DD we last counted under
string   g_last_copy_text   = "—";     // "BUY EURUSD 0.10 · 14:23"
datetime g_last_copy_time   = 0;
string   g_last_status      = "live";  // "live" | "blocked" | "stopped"
string   g_last_error       = "";
datetime g_last_poll_ok     = 0;

// Object-name prefix so we can delete only our objects on shutdown.
#define PNL "AGC_pnl_"

// Map of source bot's trade_id → user's MT5 position ticket. Persisted
// via Terminal global variables so a restart can still close the
// right positions when CLOSE events arrive later.
#define GV_PREFIX  "AGCopier_map_"
#define GV_BOOKMARK "AGCopier_bookmark"

int OnInit()
{
   if(StringLen(ApiToken) < 10)
   {
      Print("AntiGreedCopier: ApiToken is empty or too short — paste the key from your dashboard.");
      return INIT_PARAMETERS_INCORRECT;
   }
   trade.SetExpertMagicNumber(Magic);
   trade.SetTypeFillingBySymbol(Symbol());

   // Restore last bookmark so we don't double-process events on restart.
   if(GlobalVariableCheck(GV_BOOKMARK))
   {
      double cached = GlobalVariableGet(GV_BOOKMARK);
      // We stash the bookmark as a string in a chart object instead —
      // MT5 globals are numeric only. Read from there.
   }
   g_bookmark = ReadBookmark();
   ParseEnabledSymbols();
   Print("AntiGreedCopier started · base=", ApiBaseUrl,
         " · token=", StringSubstr(ApiToken, 0, 8), "..." ,
         " · symbols=", (g_filter_active ? IntegerToString(g_enabled_count) : "all"),
         " · bookmark=", (g_bookmark == "" ? "<none>" : g_bookmark));

   if(ShowPanel) BuildPanel();
   EventSetTimer(MathMax(2, PollSeconds));
   Poll();
   ReportAccount();  // first snapshot immediately so the dashboard lights up
   if(ShowPanel) RedrawPanel();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectsDeleteAll(0, PNL);
   ChartRedraw();
}

void OnTimer()
{
   Poll();
   MaybeReportAccount();
   // Catch broker-side closures (SL/TP hits, manual MT5 close) that
   // didn't go through HandleClose — without this they'd stay marked
   // OPEN on the dashboard forever.
   ReconcileClosedPositions();
   if(ShowPanel) RedrawPanel();
}

void OnTick()
{
   // Pure timer-driven — OnTick is a no-op.
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   // Re-center the panel when the chart window resizes.
   if(id == CHARTEVENT_CHART_CHANGE && ShowPanel)
   {
      BuildPanel();
      RedrawPanel();
   }
}

//+------------------------------------------------------------------+
//| Poll the signal feed and dispatch events                          |
//+------------------------------------------------------------------+
void Poll()
{
   string url = ApiBaseUrl + "/signals/feed";
   if(StringLen(g_bookmark) > 0)
      url = url + "?since=" + UrlEncode(g_bookmark);

   string headers = "Authorization: Bearer " + ApiToken + "\r\n" +
                    "Content-Type: application/json\r\n";
   char post[];
   char result[];
   string result_headers;
   ResetLastError();
   int code = WebRequest("GET", url, headers, 8000, post, result, result_headers);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014)
      {
         Print("AntiGreedCopier: WebRequest blocked — add ",
               ApiBaseUrl, " to Tools → Options → Expert Advisors → Allow WebRequest.");
         g_last_status = "blocked";
         g_last_error  = "WebRequest needs URL whitelist";
      }
      else if(Verbose)
      {
         Print("AntiGreedCopier: WebRequest error ", err);
         g_last_status = "stopped";
         g_last_error  = "WebRequest error " + IntegerToString(err);
      }
      return;
   }
   if(code != 200)
   {
      Print("AntiGreedCopier: HTTP ", code, " from /signals/feed");
      g_last_status = "stopped";
      g_last_error  = "HTTP " + IntegerToString(code);
      return;
   }
   g_last_status   = "live";
   g_last_error    = "";
   g_last_poll_ok  = TimeCurrent();

   string body = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   string new_bookmark = JsonStringField(body, "bookmark");
   if(StringLen(new_bookmark) > 0 && new_bookmark != g_bookmark)
   {
      g_bookmark = new_bookmark;
      WriteBookmark(g_bookmark);
   }

   // Walk every event object. The feed returns them oldest-first.
   string events_segment = JsonExtractArray(body, "events");
   if(StringLen(events_segment) == 0) return;

   int pos = 0;
   while(true)
   {
      string obj = JsonNextObject(events_segment, pos);
      if(StringLen(obj) == 0) break;
      DispatchEvent(obj);
   }
}

//+------------------------------------------------------------------+
//| Decide what to do with one event                                  |
//+------------------------------------------------------------------+
void DispatchEvent(const string &obj)
{
   string type = JsonStringField(obj, "type");
   long trade_id = (long)JsonNumberField(obj, "trade_id");
   string symbol_raw = JsonStringField(obj, "symbol");
   string symbol = MapSymbol(symbol_raw);
   string side = JsonStringField(obj, "side");
   double lot_in = JsonNumberField(obj, "lot_size");
   double price  = JsonNumberField(obj, "price");
   double sl     = JsonNumberField(obj, "stop_loss");
   double tp     = JsonNumberField(obj, "take_profit");

   if(type == "OPEN")
      HandleOpen(trade_id, symbol, side, lot_in, sl, tp);
   else if(type == "CLOSE")
      HandleClose(trade_id, symbol);
}

void HandleOpen(long trade_id, const string &symbol, const string &side,
                double lot_in, double sl, double tp)
{
   if(GlobalVariableCheck(GV_PREFIX + (string)trade_id))
   {
      if(Verbose) Print("AntiGreedCopier: skip duplicate OPEN trade_id=", trade_id);
      return;
   }
   // Whitelist filter — when EnabledSymbols is set, skip anything not
   // on the list. Match against both the source symbol (pre-suffix-map)
   // and the resolved local symbol so users can write either form.
   if(g_filter_active && !IsSymbolEnabled(symbol))
   {
      if(Verbose) Print("AntiGreedCopier: ", symbol, " not in EnabledSymbols — skipping.");
      return;
   }
   if(!SymbolSelect(symbol, true))
   {
      Print("AntiGreedCopier: symbol ", symbol, " not available on this broker — skipping.");
      return;
   }
   double lot = NormalizeLot(symbol, lot_in * RiskMultiplier);
   if(lot <= 0)
   {
      Print("AntiGreedCopier: computed lot 0 for ", symbol, " — skipping.");
      return;
   }
   double use_sl = (PlaceStopLoss && sl > 0) ? sl : 0;
   double use_tp = (PlaceTakeProfit && tp > 0) ? tp : 0;
   bool ok = false;
   string comment = StringFormat("AGcopy #%I64d", trade_id);
   if(side == "BUY")
      ok = trade.Buy(lot, symbol, 0, use_sl, use_tp, comment);
   else if(side == "SELL")
      ok = trade.Sell(lot, symbol, 0, use_sl, use_tp, comment);
   if(!ok)
   {
      Print("AntiGreedCopier: OPEN failed for ", side, " ", symbol,
            " lot=", lot, " retcode=", trade.ResultRetcode(),
            " (", trade.ResultRetcodeDescription(), ")");
      return;
   }
   ulong ticket = trade.ResultOrder();
   if(ticket == 0) ticket = trade.ResultDeal();
   // Persist trade_id → ticket so a restart can still find the position
   // when CLOSE arrives later.
   GlobalVariableSet(GV_PREFIX + (string)trade_id, (double)ticket);
   if(Verbose)
      Print("AntiGreedCopier: OPEN ", side, " ", symbol, " ", lot, " lot ticket=", ticket);
   // Report the OPEN fill to the dashboard so the operator's Trades
   // view reflects their own MT5 reality (lot/price/currency) instead
   // of admin's bot journal. Best-effort — a failed POST here doesn't
   // affect the trade itself.
   double fill_price = trade.ResultPrice();
   if(fill_price <= 0) fill_price = SymbolInfoDouble(symbol, side == "BUY" ? SYMBOL_ASK : SYMBOL_BID);
   ReportFillOpen((long)ticket, trade_id, symbol, side, lot,
                  fill_price, use_sl, use_tp, TimeCurrent());
   // Panel counters
   MaybeResetDailyCounter();
   g_copies_today++;
   g_last_copy_time = TimeCurrent();
   g_last_copy_text = StringFormat("%s %s %s",
      side, symbol, DoubleToString(lot, 2));
   if(ShowPanel) RedrawPanel();
}

void HandleClose(long trade_id, const string &symbol)
{
   string key = GV_PREFIX + (string)trade_id;
   if(!GlobalVariableCheck(key))
   {
      if(Verbose) Print("AntiGreedCopier: CLOSE for unknown trade_id=", trade_id, " (already closed?)");
      return;
   }
   ulong ticket = (ulong)GlobalVariableGet(key);
   if(!PositionSelectByTicket(ticket))
   {
      // Position already gone — clean up the map.
      GlobalVariableDel(key);
      return;
   }
   if(!trade.PositionClose(ticket))
   {
      Print("AntiGreedCopier: CLOSE failed ticket=", ticket,
            " retcode=", trade.ResultRetcode(),
            " (", trade.ResultRetcodeDescription(), ")");
      return;
   }
   GlobalVariableDel(key);
   if(Verbose) Print("AntiGreedCopier: CLOSED ticket=", ticket, " for trade_id=", trade_id);
   // Report the CLOSED fill with the operator's actual broker pnl. We
   // read it from MT5 deal history so it matches the broker statement
   // exactly (includes swap + commission).
   ReportFillClosed((long)ticket, trade_id, "master_close");
}

//+------------------------------------------------------------------+
//| Symbol mapping — broker suffixes (e.g. "m" for Exness micro)      |
//+------------------------------------------------------------------+
string MapSymbol(const string &src)
{
   if(StringLen(SymbolSuffix) == 0) return src;
   // Source uses "EURUSDm" but local broker uses "EURUSD" (or vice versa) —
   // if the source symbol already ends with the suffix and the local
   // broker doesn't need it, strip; otherwise append.
   if(StringLen(src) > StringLen(SymbolSuffix) &&
      StringSubstr(src, StringLen(src) - StringLen(SymbolSuffix)) == SymbolSuffix)
   {
      // Source already has the suffix — try as-is first.
      if(SymbolSelect(src, true)) return src;
      // Strip and try.
      return StringSubstr(src, 0, StringLen(src) - StringLen(SymbolSuffix));
   }
   // Source has no suffix — try appending.
   string candidate = src + SymbolSuffix;
   if(SymbolSelect(candidate, true)) return candidate;
   return src;
}

double NormalizeLot(const string &symbol, double lot)
{
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minl = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxl = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0) step = 0.01;
   if(minl <= 0) minl = 0.01;
   double cap = MathMin(MaxLotPerTrade, maxl > 0 ? maxl : MaxLotPerTrade);
   if(lot < minl) return 0; // skip, too small
   if(lot > cap)  lot = cap;
   lot = MathFloor(lot / step) * step;
   if(lot < minl) return 0;
   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
//| Bookmark persistence — MT5 globals are doubles only, so we store  |
//| the ISO string on a hidden chart-window text object.              |
//+------------------------------------------------------------------+
#define BOOKMARK_OBJ "AGCopier_BookmarkObj"

void WriteBookmark(const string &iso)
{
   if(ObjectFind(0, BOOKMARK_OBJ) < 0)
      ObjectCreate(0, BOOKMARK_OBJ, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, BOOKMARK_OBJ, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, BOOKMARK_OBJ, OBJPROP_CORNER, CORNER_RIGHT_LOWER);
   ObjectSetInteger(0, BOOKMARK_OBJ, OBJPROP_XDISTANCE, -9999);  // off-screen
   ObjectSetString(0, BOOKMARK_OBJ, OBJPROP_TEXT, iso);
}

string ReadBookmark()
{
   if(ObjectFind(0, BOOKMARK_OBJ) < 0) return "";
   return ObjectGetString(0, BOOKMARK_OBJ, OBJPROP_TEXT);
}

//+------------------------------------------------------------------+
//| Tiny pull-only JSON helpers — sufficient for our flat shapes.     |
//+------------------------------------------------------------------+
string JsonStringField(const string &json, const string &field)
{
   string needle = "\"" + field + "\":";
   int i = StringFind(json, needle);
   if(i < 0) return "";
   i += StringLen(needle);
   while(i < StringLen(json) && (StringGetCharacter(json, i) == ' ' ||
         StringGetCharacter(json, i) == '\t')) i++;
   if(i >= StringLen(json)) return "";
   if(StringGetCharacter(json, i) != '"')
   {
      // Could be null
      if(StringSubstr(json, i, 4) == "null") return "";
      return "";
   }
   int start = i + 1;
   int end = start;
   while(end < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == '\\') { end += 2; continue; }
      if(ch == '"') break;
      end++;
   }
   return StringSubstr(json, start, end - start);
}

double JsonNumberField(const string &json, const string &field)
{
   string needle = "\"" + field + "\":";
   int i = StringFind(json, needle);
   if(i < 0) return 0;
   i += StringLen(needle);
   while(i < StringLen(json) && (StringGetCharacter(json, i) == ' ' ||
         StringGetCharacter(json, i) == '\t')) i++;
   if(StringSubstr(json, i, 4) == "null") return 0;
   int start = i;
   while(i < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, i);
      if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-' || ch == '+' ||
         ch == 'e' || ch == 'E')
      { i++; continue; }
      break;
   }
   return StringToDouble(StringSubstr(json, start, i - start));
}

// Extract the substring inside the brackets of "events": [...] so we
// can walk objects with JsonNextObject. Returns the inner contents
// (without the brackets) or "" if the key is absent.
string JsonExtractArray(const string &json, const string &field)
{
   string needle = "\"" + field + "\":";
   int i = StringFind(json, needle);
   if(i < 0) return "";
   i += StringLen(needle);
   while(i < StringLen(json) && (StringGetCharacter(json, i) == ' ' ||
         StringGetCharacter(json, i) == '\t')) i++;
   if(i >= StringLen(json) || StringGetCharacter(json, i) != '[') return "";
   int depth = 1;
   int start = i + 1;
   int end = start;
   while(end < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == '[') depth++;
      else if(ch == ']') { depth--; if(depth == 0) break; }
      end++;
   }
   return StringSubstr(json, start, end - start);
}

// Pull the next {...} block out of an array body, advancing `pos`.
// Returns "" when no more objects remain. Caller seeds pos=0.
string JsonNextObject(const string &arr, int &pos)
{
   while(pos < StringLen(arr) && StringGetCharacter(arr, pos) != '{') pos++;
   if(pos >= StringLen(arr)) return "";
   int depth = 0;
   int start = pos;
   while(pos < StringLen(arr))
   {
      ushort ch = StringGetCharacter(arr, pos);
      if(ch == '{') depth++;
      else if(ch == '}')
      {
         depth--;
         if(depth == 0) { pos++; return StringSubstr(arr, start, pos - start); }
      }
      pos++;
   }
   return "";
}

//+------------------------------------------------------------------+
//| Account reporting — POST balance/equity/etc. to /me/ea-account    |
//| so the dashboard can show this operator's own numbers instead of  |
//| the admin master account.                                          |
//+------------------------------------------------------------------+
void MaybeReportAccount()
{
   if(AccountReportSeconds <= 0) return;
   datetime now = TimeCurrent();
   if(g_last_account_report > 0 &&
      (now - g_last_account_report) < AccountReportSeconds) return;
   ReportAccount();
}

void ReportAccount()
{
   double balance     = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity      = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin      = AccountInfoDouble(ACCOUNT_MARGIN);
   double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   long   login       = AccountInfoInteger(ACCOUNT_LOGIN);
   string server      = AccountInfoString(ACCOUNT_SERVER);
   string company     = AccountInfoString(ACCOUNT_COMPANY);
   string currency    = AccountInfoString(ACCOUNT_CURRENCY);

   string body = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,"
      "\"free_margin\":%.2f,\"login\":%I64d,"
      "\"server\":\"%s\",\"broker\":\"%s\",\"currency\":\"%s\"}",
      balance, equity, margin, free_margin, login,
      JsonEscape(server), JsonEscape(company), JsonEscape(currency));

   string url = ApiBaseUrl + "/me/ea-account";
   string headers = "Authorization: Bearer " + ApiToken + "\r\n" +
                    "Content-Type: application/json\r\n";
   char post[];
   StringToCharArray(body, post, 0, StringLen(body), CP_UTF8);
   char result[];
   string result_headers;
   ResetLastError();
   int code = WebRequest("POST", url, headers, 8000, post, result, result_headers);
   if(code == 200)
   {
      g_last_account_report = TimeCurrent();
      if(Verbose) Print("AntiGreedCopier: account snapshot reported.");
   }
   else if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014 && Verbose)
         Print("AntiGreedCopier: account report blocked — WebRequest needs ", ApiBaseUrl, " whitelisted.");
   }
   else if(Verbose)
   {
      Print("AntiGreedCopier: account report HTTP ", code);
   }
}

//+------------------------------------------------------------------+
//| Fill reporting — POST each open/close to /me/ea-fill so the       |
//| dashboard's Trades view shows the operator's own MT5 reality      |
//| (lot, fill price, broker pnl in account currency) instead of      |
//| admin's bot journal.                                              |
//+------------------------------------------------------------------+
void ReportFillOpen(long ticket, long trade_id, const string &symbol,
                    const string &side, double lot, double entry_price,
                    double sl, double tp, datetime opened_at)
{
   string body = StringFormat(
      "{\"broker_ticket\":%I64d,\"master_trade_id\":%I64d,"
      "\"symbol\":\"%s\",\"side\":\"%s\",\"lot_size\":%.4f,"
      "\"entry_price\":%.8f,\"stop_loss\":%.8f,\"take_profit\":%.8f,"
      "\"status\":\"OPEN\",\"opened_at\":\"%s\"}",
      ticket, trade_id, JsonEscape(symbol), side, lot, entry_price,
      sl, tp, IsoTimestamp(opened_at));
   PostFill(body);
}

void ReportFillClosed(long ticket, long trade_id, const string &close_reason)
{
   // Pull the actual broker-computed pnl + exit price from MT5 history.
   // History only becomes visible after the broker confirms the close,
   // so we may need a quick retry — keep it short to avoid blocking
   // the timer.
   double   exit_price = 0.0;
   double   pnl_total  = 0.0;
   datetime closed_at  = TimeCurrent();
   datetime opened_at  = closed_at;
   string   sym        = "";
   string   side       = "BUY";
   double   lot        = 0.0;
   double   entry_price = 0.0;
   bool found = ReadCloseFromHistory(ticket, sym, side, lot, entry_price,
                                     opened_at, exit_price, pnl_total, closed_at);
   if(!found)
   {
      // History not visible yet — try again next OnTimer via reconcile.
      // Don't POST a half-filled row that would clobber the OPEN we
      // already reported.
      if(Verbose) Print("AntiGreedCopier: history not ready for ticket=",
                       ticket, " — will retry on next reconcile.");
      // Re-stash the ticket so reconcile picks it back up.
      GlobalVariableSet(GV_PREFIX + (string)trade_id, (double)ticket);
      return;
   }
   string body = StringFormat(
      "{\"broker_ticket\":%I64d,\"master_trade_id\":%I64d,"
      "\"symbol\":\"%s\",\"side\":\"%s\",\"lot_size\":%.4f,"
      "\"entry_price\":%.8f,\"exit_price\":%.8f,\"pnl\":%.2f,"
      "\"status\":\"CLOSED\",\"close_reason\":\"%s\","
      "\"opened_at\":\"%s\",\"closed_at\":\"%s\"}",
      ticket, trade_id,
      JsonEscape(sym), side, lot,
      entry_price, exit_price, pnl_total,
      JsonEscape(close_reason),
      IsoTimestamp(opened_at), IsoTimestamp(closed_at));
   PostFill(body);
}

void PostFill(const string &body)
{
   string url = ApiBaseUrl + "/me/ea-fill";
   string headers = "Authorization: Bearer " + ApiToken + "\r\n" +
                    "Content-Type: application/json\r\n";
   char post[];
   StringToCharArray(body, post, 0, StringLen(body), CP_UTF8);
   char result[];
   string result_headers;
   ResetLastError();
   int code = WebRequest("POST", url, headers, 8000, post, result, result_headers);
   if(code == 200)
   {
      if(Verbose) Print("AntiGreedCopier: fill reported ok.");
   }
   else if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014 && Verbose)
         Print("AntiGreedCopier: fill report blocked — WebRequest needs ", ApiBaseUrl, " whitelisted.");
   }
   else if(Verbose)
   {
      Print("AntiGreedCopier: fill report HTTP ", code, " body=", CharArrayToString(result));
   }
}

// Read the operator-side close result from MT5 deal history. Pulls
// every deal tagged to this position, captures the IN leg's symbol /
// side / volume / entry price, and sums the OUT legs' profit + swap +
// commission for total pnl. Returns false when the broker hasn't yet
// committed the close deals — the reconcile pass will retry on the
// next OnTimer tick.
bool ReadCloseFromHistory(long ticket,
                          string &sym, string &side, double &lot,
                          double &entry_price, datetime &opened_at,
                          double &exit_price, double &pnl_total,
                          datetime &closed_at)
{
   if(!HistorySelectByPosition(ticket)) return false;
   int total = HistoryDealsTotal();
   double sum_profit = 0.0;
   double sum_swap   = 0.0;
   double sum_comm   = 0.0;
   datetime last_time = 0;
   double last_price  = 0.0;
   bool any_in  = false;
   bool any_out = false;
   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      long entry_type = HistoryDealGetInteger(deal, DEAL_ENTRY);
      sum_profit += HistoryDealGetDouble(deal, DEAL_PROFIT);
      sum_swap   += HistoryDealGetDouble(deal, DEAL_SWAP);
      sum_comm   += HistoryDealGetDouble(deal, DEAL_COMMISSION);
      if(entry_type == DEAL_ENTRY_IN && !any_in)
      {
         any_in      = true;
         sym         = HistoryDealGetString(deal, DEAL_SYMBOL);
         long dtype  = HistoryDealGetInteger(deal, DEAL_TYPE);
         // DEAL_TYPE_BUY/SELL: side of the *entry* deal.
         side        = (dtype == DEAL_TYPE_BUY) ? "BUY" : "SELL";
         lot         = HistoryDealGetDouble(deal, DEAL_VOLUME);
         entry_price = HistoryDealGetDouble(deal, DEAL_PRICE);
         opened_at   = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
      }
      if(entry_type == DEAL_ENTRY_OUT || entry_type == DEAL_ENTRY_INOUT)
      {
         any_out = true;
         datetime t = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
         if(t >= last_time)
         {
            last_time = t;
            last_price = HistoryDealGetDouble(deal, DEAL_PRICE);
         }
      }
   }
   if(!any_out || !any_in) return false;
   exit_price = last_price;
   pnl_total  = sum_profit + sum_swap + sum_comm;
   closed_at  = last_time;
   return true;
}

// ISO-8601 UTC timestamp matching what the server expects in
// EAFillReportRequest.opened_at / closed_at.
string IsoTimestamp(datetime t)
{
   MqlDateTime mdt;
   TimeToStruct(t, mdt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
                       mdt.year, mdt.mon, mdt.day,
                       mdt.hour, mdt.min, mdt.sec);
}

//+------------------------------------------------------------------+
//| Reconcile positions that closed without our HandleClose firing    |
//| — typically because MT5 hit the broker-side SL/TP. Walks every    |
//| tracked trade_id → ticket mapping; if the position is gone, we    |
//| send a CLOSED report and delete the global so we don't keep       |
//| reporting it.                                                     |
//+------------------------------------------------------------------+
void ReconcileClosedPositions()
{
   int total = GlobalVariablesTotal();
   // Collect into a list first — GlobalVariableDel inside the loop
   // would shift indices and skip entries.
   long   tickets_to_close[];
   long   trade_ids[];
   ArrayResize(tickets_to_close, 0);
   ArrayResize(trade_ids, 0);
   int prefix_len = StringLen(GV_PREFIX);
   for(int i = 0; i < total; i++)
   {
      string name = GlobalVariableName(i);
      if(StringSubstr(name, 0, prefix_len) != GV_PREFIX) continue;
      long trade_id = StringToInteger(StringSubstr(name, prefix_len));
      ulong ticket = (ulong)GlobalVariableGet(name);
      if(ticket == 0) continue;
      if(PositionSelectByTicket(ticket)) continue;  // still open
      int n = ArraySize(tickets_to_close);
      ArrayResize(tickets_to_close, n + 1);
      ArrayResize(trade_ids, n + 1);
      tickets_to_close[n] = (long)ticket;
      trade_ids[n]        = trade_id;
   }
   for(int j = 0; j < ArraySize(tickets_to_close); j++)
   {
      ReportFillClosed(tickets_to_close[j], trade_ids[j], "broker_close");
      GlobalVariableDel(GV_PREFIX + (string)trade_ids[j]);
      if(Verbose)
         Print("AntiGreedCopier: reconciled close for ticket=",
               tickets_to_close[j], " trade_id=", trade_ids[j]);
   }
}

// Minimal JSON-string escape — handles backslash and quote so broker
// names with apostrophes etc. don't break the payload.
string JsonEscape(const string &s)
{
   string out = "";
   for(int i = 0; i < StringLen(s); i++)
   {
      ushort ch = StringGetCharacter(s, i);
      if(ch == '\\' || ch == '"') out += "\\";
      out += ShortToString(ch);
   }
   return out;
}

string UrlEncode(const string &s)
{
   string out = "";
   for(int i = 0; i < StringLen(s); i++)
   {
      ushort ch = StringGetCharacter(s, i);
      if((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
         (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '.' || ch == '~')
         out += ShortToString(ch);
      else
         out += StringFormat("%%%02X", ch);
   }
   return out;
}

//+==================================================================+
//|                      SYMBOL WHITELIST                              |
//+==================================================================+
void ParseEnabledSymbols()
{
   ArrayResize(g_enabled_symbols, 0);
   g_enabled_count = 0;
   string s = EnabledSymbols;
   StringTrimLeft(s); StringTrimRight(s);
   if(StringLen(s) == 0)
   {
      g_filter_active = false;
      return;
   }
   g_filter_active = true;
   string parts[];
   int n = StringSplit(s, (ushort)',', parts);
   for(int i = 0; i < n; i++)
   {
      string p = parts[i];
      StringTrimLeft(p); StringTrimRight(p);
      StringToUpper(p);
      if(StringLen(p) == 0) continue;
      int sz = ArraySize(g_enabled_symbols);
      ArrayResize(g_enabled_symbols, sz + 1);
      g_enabled_symbols[sz] = p;
      g_enabled_count++;
   }
}

bool IsSymbolEnabled(const string &symbol)
{
   if(!g_filter_active) return true;
   string up = symbol; StringToUpper(up);
   // Match the local symbol as well as the stripped form so users can
   // list either "EURUSD" or "EURUSDm" — whichever they're familiar with.
   string stripped = up;
   if(StringLen(SymbolSuffix) > 0 &&
      StringLen(up) > StringLen(SymbolSuffix))
   {
      string sufx = SymbolSuffix; StringToUpper(sufx);
      if(StringSubstr(up, StringLen(up) - StringLen(sufx)) == sufx)
         stripped = StringSubstr(up, 0, StringLen(up) - StringLen(sufx));
   }
   for(int i = 0; i < g_enabled_count; i++)
   {
      if(g_enabled_symbols[i] == up) return true;
      if(g_enabled_symbols[i] == stripped) return true;
   }
   return false;
}

//+==================================================================+
//|                      ON-CHART PANEL                                |
//+==================================================================+
// Layout constants — px from the panel's top-left corner. Generous
// padding/gaps so labels, numbers and section dividers never run into
// each other regardless of font size.
#define P_PAD          22
#define P_HEADER_H     74
#define P_ROW_H        22
#define P_SECTION_GAP  22
#define P_FONT         "Consolas"
#define P_FONT_BODY    "Segoe UI"

// Panel width (live; pulled from PanelWidth input on init) and its
// computed top-left position in chart pixels. Refreshed whenever the
// chart resizes via OnChartEvent.
int g_panel_w = 620;
int g_panel_x = 0;
int g_panel_y = 0;

// Object builders ---------------------------------------------------
// We use OBJ_BUTTON for ALL filled rectangles. OBJ_RECTANGLE_LABEL's
// fill is unreliable on Wine MT5 (the chart shows through). Buttons
// fill consistently across native Windows and Wine, and with
// READONLY+disabled-state they're visually inert.
void MakeBox(const string name, int xoff, int yoff, int xsize, int ysize,
             color bg, color border, const string text = "",
             color text_clr = clrWhite, int text_size = 9,
             const string text_font = "Segoe UI",
             ENUM_ALIGN_MODE align = ALIGN_LEFT)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, xoff);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, yoff);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, xsize);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, ysize);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, name, OBJPROP_BORDER_COLOR, border);
   ObjectSetInteger(0, name, OBJPROP_COLOR, text_clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, text_size);
   ObjectSetString (0, name, OBJPROP_FONT, text_font);
   ObjectSetString (0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_ALIGN, align);
   ObjectSetInteger(0, name, OBJPROP_STATE, false);
   ObjectSetInteger(0, name, OBJPROP_READONLY, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, name, OBJPROP_ZORDER, 100);
}

void MakeLabel(const string name, int xoff, int yoff, const string text,
               color clr, int size, const string font, ENUM_ANCHOR_POINT anchor = ANCHOR_LEFT_UPPER)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, xoff);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, yoff);
   ObjectSetString (0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetString (0, name, OBJPROP_FONT, font);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_BACK, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   // Labels go ABOVE the filled boxes from MakeBox.
   ObjectSetInteger(0, name, OBJPROP_ZORDER, 200);
}

void MakeBitmap(const string name, int xoff, int yoff, const string file, int xsize, int ysize)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_BITMAP_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, xoff);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, yoff);
   ObjectSetString (0, name, OBJPROP_BMPFILE, 0, "\\Files\\" + file);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, xsize);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, ysize);
   ObjectSetInteger(0, name, OBJPROP_BACK, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
}

// Sizing ------------------------------------------------------------
// Layout heights for the Figma-inspired sections.
#define P_HERO_H       220  // big balance number + label + last-sync line
#define P_TILE_H       180  // KPI tile (3-up row)
#define P_BIGTILE_H    220  // larger info tiles (2-up row)
#define P_FOOTER_H     36   // developer attribution line
#define P_GRAD_H       12   // bottom rainbow gradient strip

int ComputePanelHeight()
{
   // User can force a specific height via the PanelHeight input. 0 (the
   // default) auto-fits to the section content.
   if(PanelHeight > 0) return PanelHeight;
   return P_HEADER_H
        + P_SECTION_GAP
        + P_HERO_H
        + P_SECTION_GAP
        + P_TILE_H
        + P_SECTION_GAP
        + P_BIGTILE_H
        + P_SECTION_GAP
        + P_FOOTER_H
        + P_GRAD_H
        + 4;
}

// Recompute where the panel's top-left should sit on the chart, based
// on PanelCentered + PanelWidth + nudge offsets. Called on init and
// whenever the chart resizes.
void LayoutPanel()
{
   g_panel_w = MathMax(280, PanelWidth);
   int h = ComputePanelHeight();
   if(PanelCentered)
   {
      int cw = (int)ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
      int ch = (int)ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
      // Centered horizontally + vertically — falls back to a safe top-left
      // if MT5 hasn't reported chart dimensions yet.
      g_panel_x = (cw > g_panel_w ? (cw - g_panel_w) / 2 : 20) + PanelOffsetX;
      g_panel_y = (ch > h         ? (ch - h)         / 2 : 20) + PanelOffsetY;
   }
   else
   {
      g_panel_x = MathMax(0, PanelOffsetX);
      g_panel_y = MathMax(0, PanelOffsetY);
   }
}

// ===== Figma-inspired palette ======================================
// All colours kept as C'r,g,b' literals so they're easy to read/tweak.
#define COL_BG          C'14,18,32'      // panel backdrop
#define COL_BG_2        C'22,26,42'      // header band (slightly lifted)
#define COL_BG_TILE     C'24,30,48'      // tile body
#define COL_TXT         C'232,240,255'   // body text
#define COL_MUTED       C'130,148,176'   // labels
#define COL_HERO        C'255,128,90'    // hero number (orange-coral)
#define COL_GREEN       C'80,220,150'    // positive / buy / balance
#define COL_RED         C'255,100,120'   // negative / sell / drawdown
#define COL_CYAN        C'88,210,231'    // primary accent
#define COL_BLUE        C'120,170,255'   // info tile
#define COL_PURPLE      C'170,130,255'   // accent for the bottom strip
#define COL_GREEN_TILE  C'24,52,40'      // green-tinted tile body
#define COL_RED_TILE    C'54,26,36'      // red-tinted tile body
#define COL_BLUE_TILE   C'28,38,68'      // blue/purple-tinted tile body
#define COL_PANEL_EDGE  C'48,62,92'      // soft inner border for sections

// One-time build — everything else is a redraw of text values.
void BuildPanel()
{
   ObjectsDeleteAll(0, PNL);
   LayoutPanel();
   int h = ComputePanelHeight();
   int x = g_panel_x;
   int y = g_panel_y;

   // ── Backdrop (chart can't bleed through). ─────────────────────────
   MakeBox(PNL + "card",       x, y, g_panel_w, h, COL_BG, COL_BG_2);
   // Vertical neon brand stripe on the very left edge — visible even
   // at a glance, tells you which panel this is.
   MakeBox(PNL + "leftbar",    x, y, 5, h, COL_CYAN, COL_CYAN);

   // ── Header band ───────────────────────────────────────────────────
   MakeBox(PNL + "hdr",        x, y, g_panel_w, P_HEADER_H, COL_BG_2, COL_BG_2);
   // Soft 1-px divider line between header and body for more polish.
   MakeBox(PNL + "hdr_div",    x, y + P_HEADER_H, g_panel_w, 1,
           COL_PANEL_EDGE, COL_PANEL_EDGE);

   // Big "AG" badge — bumped 36 -> 44 so the brand reads at a glance.
   MakeBox(PNL + "badge",      x + 20, y + 14, 44, 44, COL_CYAN, COL_CYAN);
   MakeLabel(PNL + "badge_t",  x + 42, y + 20, "AG",
             C'10,18,28', 18, "Segoe UI Black", ANCHOR_UPPER);

   // Title + sub-line.
   MakeLabel(PNL + "title", x + 76, y + 18,
             "LIVE TRADE COPIER", COL_TXT, 14, "Segoe UI Semibold");
   MakeLabel(PNL + "sub",   x + 76, y + 44,
             "AntiGreed signal mirror · " + EA_BUILD, COL_MUTED, 9, P_FONT_BODY);

   // Status pill on the right (dynamic colour set in RedrawPanel).
   int pill_w = 120;
   MakeBox(PNL + "pill",         x + g_panel_w - pill_w - 18, y + 22, pill_w, 30,
           C'42,22,32', COL_RED);
   MakeLabel(PNL + "pill_arrow", x + g_panel_w - pill_w - 4,  y + 28,
             "↘", COL_RED, 12, "Segoe UI Semibold");
   MakeLabel(PNL + "pill_t",     x + g_panel_w - pill_w + 24, y + 28,
             "OFFLINE", COL_RED, 10, "Segoe UI Semibold");
}

string ShortTime(datetime t)
{
   if(t == 0) return "—";
   return TimeToString(t, TIME_MINUTES);
}

void MaybeResetDailyCounter()
{
   string today = TimeToString(TimeCurrent(), TIME_DATE);
   if(today != g_today_key)
   {
      g_today_key      = today;
      g_copies_today   = 0;
   }
}

// Count our own positions live so the panel always tells the truth.
int OurOpenPositions()
{
   int n = 0;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong tkt = PositionGetTicket(i);
      if(tkt == 0 || !PositionSelectByTicket(tkt)) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) == Magic) n++;
   }
   return n;
}

void RedrawPanel()
{
   MaybeResetDailyCounter();
   int x = g_panel_x;

   // ── Status pill colours derive from health. ──────────────────────
   color st_clr = COL_GREEN;
   color st_bg  = C'18,42,32';
   string st_text  = "LIVE";
   string st_arrow = "↗";
   if(g_last_status == "blocked")
   {
      st_clr = C'255,179,0'; st_bg = C'52,38,8';
      st_text = "BLOCKED"; st_arrow = "⚠";
   }
   else if(g_last_status == "stopped")
   {
      st_clr = COL_RED; st_bg = C'46,22,30';
      st_text = "OFFLINE"; st_arrow = "↘";
   }
   ObjectSetInteger(0, PNL + "pill",      OBJPROP_BGCOLOR, st_bg);
   ObjectSetInteger(0, PNL + "pill",      OBJPROP_BORDER_COLOR, st_clr);
   ObjectSetInteger(0, PNL + "pill_t",    OBJPROP_COLOR, st_clr);
   ObjectSetInteger(0, PNL + "pill_arrow",OBJPROP_COLOR, st_clr);
   ObjectSetString (0, PNL + "pill_t",    OBJPROP_TEXT, st_text);
   ObjectSetString (0, PNL + "pill_arrow",OBJPROP_TEXT, st_arrow);
   string sub = "AntiGreed signal mirror · " + EA_BUILD + " · poll " +
                IntegerToString(MathMax(2, PollSeconds)) + "s";
   if(g_last_status != "live" && StringLen(g_last_error) > 0)
      sub = EA_BUILD + " · " + g_last_error;
   ObjectSetString(0, PNL + "sub", OBJPROP_TEXT, sub);

   // ── Hero block — big balance number, label, last-sync line. ──────
   int y = g_panel_y + P_HEADER_H + P_SECTION_GAP;
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double floating = eq - bal;
   string equity_pct_text = "";
   if(bal > 0.01)
   {
      double pct = (floating / bal) * 100.0;
      equity_pct_text = StringFormat("%s%.2f%%", (pct >= 0 ? "+" : ""), pct);
   }
   // Vertical layout for the hero block — every element advances the
   // y-cursor by a generous fixed amount so labels/numbers never collide.
   int hy = y + 24;
   MakeLabel(PNL + "hero_l", x + P_PAD, hy,
             "BALANCE", COL_MUTED, 10, P_FONT_BODY);
   hy += 40;  // gap below label, before the big number
   MakeLabel(PNL + "hero_v", x + P_PAD, hy,
             FmtMoney(bal, cur), COL_HERO, 32, "Segoe UI Semibold");
   // Trending-up arrow next to the big number — replaces the lightning
   // bolt glyph, which renders as a tofu box on Wine MT5's font.
   MakeLabel(PNL + "hero_bolt", x + g_panel_w - 56, hy + 6,
             "↗", COL_HERO, 32, "Segoe UI Semibold");
   hy += 90;  // big enough to keep "Last sync" clear of the number above
   string sync = (g_last_poll_ok > 0)
      ? "Last sync: " + TimeToString(g_last_poll_ok, TIME_SECONDS)
      : "Last sync: —";
   MakeLabel(PNL + "hero_sub", x + P_PAD, hy,
             sync, COL_MUTED, 9, P_FONT_BODY);

   y += P_HERO_H + P_SECTION_GAP;

   // ── 3 KPI tiles: green / red / blue tinted, with coloured top stripes
   //    + an "icon" letter badge at the top-left of each tile. ────────
   int gap = 10;
   int tile_w = (g_panel_w - (P_PAD * 2) - (gap * 2)) / 3;
   string copy_text;
   if(g_last_copy_text == "—") copy_text = "—";
   else copy_text = g_last_copy_text;

   DrawColouredTile("t_today", x + P_PAD,                    y, tile_w, P_TILE_H,
                    "$", COL_GREEN_TILE, COL_GREEN,
                    "TODAY", IntegerToString(g_copies_today));
   DrawColouredTile("t_open",  x + P_PAD + tile_w + gap,     y, tile_w, P_TILE_H,
                    "$", COL_RED_TILE,   COL_RED,
                    "OPEN", IntegerToString(OurOpenPositions()));
   DrawColouredTile("t_equity",x + P_PAD + (tile_w + gap)*2, y, tile_w, P_TILE_H,
                    "≡", COL_BLUE_TILE,  COL_BLUE,
                    "EQUITY", (equity_pct_text == "" ? FmtMoney(eq, cur) : equity_pct_text));

   y += P_TILE_H + P_SECTION_GAP;

   // ── 2 big tiles below: SYMBOLS + RISK summary. ───────────────────
   int big_w = (g_panel_w - (P_PAD * 2) - gap) / 2;
   // Left — symbols summary, coloured like a BUY signal tile (green).
   string sym_value = g_filter_active
      ? IntegerToString(g_enabled_count)
      : "ALL";
   string sym_sub = g_filter_active
      ? "Filtered whitelist"
      : "Mirror every admin trade";
   DrawSignalTile("s_buy",  x + P_PAD,                  y, big_w, P_BIGTILE_H,
                  COL_GREEN_TILE, COL_GREEN, "SYMBOLS",
                  sym_value, sym_sub);
   // Right — risk summary, coloured like a SELL signal tile (red).
   string risk_value = StringFormat("x%.2f", RiskMultiplier);
   string risk_sub   = StringFormat("max %.2f lot per trade", MaxLotPerTrade);
   DrawSignalTile("s_sell", x + P_PAD + big_w + gap,    y, big_w, P_BIGTILE_H,
                  COL_RED_TILE, COL_RED, "RISK",
                  risk_value, risk_sub);

   y += P_BIGTILE_H + P_SECTION_GAP;

   // Developer attribution — centered above the gradient strip.
   MakeLabel(PNL + "footer", x + g_panel_w / 2, y + 4,
             "Developed by Martin Kristof", COL_MUTED, 8, P_FONT_BODY,
             ANCHOR_UPPER);

   y += P_FOOTER_H;

   // ── Bottom rainbow strip — cyan → indigo → purple, faked with
   //    stacked narrow rectangles. The Figma "glow" along the bottom. ─
   DrawGradientStrip(x, y, g_panel_w, P_GRAD_H);

   ChartRedraw();
}

// One of the three coloured KPI tiles. Top stripe in the accent color,
// dollar/icon glyph in the top-left, label below it, big number at bottom.
void DrawColouredTile(const string id, int xoff, int yoff, int w, int h,
                      const string icon, color bg, color accent,
                      const string label, const string value)
{
   // Backdrop + top accent stripe.
   MakeBox  (PNL + id + "_bg",   xoff,      yoff,        w, h,
             bg, accent);
   MakeBox  (PNL + id + "_top",  xoff,      yoff,        w, 4,
             accent, accent);
   // Bottom-right corner accent — small bright square, gives each tile
   // a "live indicator" feel like the Figma's glow corners.
   MakeBox  (PNL + id + "_corn", xoff + w - 12, yoff + h - 12, 6, 6,
             accent, accent);
   // Top row: icon badge on the left, label sitting to its right at
   // the badge's vertical centre. Smaller icon + shorter labels so
   // the label can't overflow the tile width even on small tiles.
   int badge_y = yoff + 22;
   MakeBox  (PNL + id + "_icon", xoff + 16, badge_y,     26, 26,
             accent, accent);
   MakeLabel(PNL + id + "_glyph",xoff + 29, badge_y + 6, icon,
             C'10,18,24', 12, "Segoe UI Black", ANCHOR_UPPER);
   MakeLabel(PNL + id + "_l",    xoff + 50, badge_y + 7, label,
             COL_MUTED, 10, P_FONT_BODY);
   // Big number sits well below the top-row, with comfortable air
   // both above (separation from label-row) and below (edge clearance).
   MakeLabel(PNL + id + "_v",    xoff + 18, yoff + h - 60, value,
             accent, 22, "Segoe UI Semibold");
}

// The two larger "BUY SIGNAL / SELL SIGNAL" style tiles at the bottom.
void DrawSignalTile(const string id, int xoff, int yoff, int w, int h,
                    color bg, color accent, const string label,
                    const string value, const string subline)
{
   MakeBox  (PNL + id + "_bg",  xoff,           yoff,           w, h,
             bg, accent);
   MakeBox  (PNL + id + "_top", xoff,           yoff,           w, 4,
             accent, accent);
   // Bottom-right corner accent matching the KPI tiles.
   MakeBox  (PNL + id + "_corn", xoff + w - 14, yoff + h - 14, 8, 8,
             accent, accent);
   // Top row: label on the left, status dot on the right.
   int top_y = yoff + 26;
   MakeLabel(PNL + id + "_l",   xoff + 22,      top_y,          label,
             accent, 11, "Segoe UI Semibold");
   MakeLabel(PNL + id + "_dot", xoff + w - 36, top_y,           "●",
             accent, 12, P_FONT_BODY);
   // Big value vertically centred — h-120 leaves room above and
   // ~80px clearance for the subline below.
   MakeLabel(PNL + id + "_v",   xoff + 22,      yoff + h - 120, value,
             accent, 28, "Segoe UI Semibold");
   // Subline pinned 36px from the tile bottom so there's a clear gap
   // between the value-row (which is ~36px tall) and the subline.
   MakeLabel(PNL + id + "_s",   xoff + 22,      yoff + h - 36,  subline,
             COL_MUTED, 10, P_FONT_BODY);
}

// Cyan → blue → purple progression along the bottom of the card. Uses
// ~24 narrow rectangles with linearly-interpolated RGB stops.
void DrawGradientStrip(int xoff, int yoff, int w, int h)
{
   int steps = 24;
   double strip_w = (double)w / (double)steps;
   // Three stops: cyan (0%) → blue (50%) → purple (100%)
   int r1 = 34,  g1 = 200, b1 = 230;   // cyan
   int r2 = 90,  g2 = 130, b2 = 245;   // mid blue
   int r3 = 185, g3 = 80,  b3 = 215;   // purple
   for(int i = 0; i < steps; i++)
   {
      double t = (double)i / (double)(steps - 1);
      int r, g, b;
      if(t < 0.5)
      {
         double u = t * 2.0;
         r = (int)(r1 + (r2 - r1) * u);
         g = (int)(g1 + (g2 - g1) * u);
         b = (int)(b1 + (b2 - b1) * u);
      }
      else
      {
         double u = (t - 0.5) * 2.0;
         r = (int)(r2 + (r3 - r2) * u);
         g = (int)(g2 + (g3 - g2) * u);
         b = (int)(b2 + (b3 - b2) * u);
      }
      color c = (color)((b & 0xFF) << 16 | (g & 0xFF) << 8 | (r & 0xFF));
      MakeBox(PNL + "g_" + IntegerToString(i),
              xoff + (int)(i * strip_w), yoff,
              (int)(strip_w) + 2, h, c, c);
   }
}

string FmtMoney(double v, const string &cur)
{
   string body = DoubleToString(v, 2);
   // Insert a thousands separator the cheap way.
   int dot = StringFind(body, ".");
   string left = (dot >= 0) ? StringSubstr(body, 0, dot) : body;
   string frac = (dot >= 0) ? StringSubstr(body, dot)    : "";
   bool neg = StringGetCharacter(left, 0) == '-';
   if(neg) left = StringSubstr(left, 1);
   string out = "";
   // Walk the integer portion (cents are in 'frac' already), inserting a
   // comma after each digit that has a multiple-of-3 remainder ahead.
   int len = StringLen(left);
   for(int i = 0; i < len; i++)
   {
      out += ShortToString(StringGetCharacter(left, i));
      int rem = len - i - 1;
      if(rem > 0 && rem % 3 == 0) out += ",";
   }
   string sym = (cur == "USD") ? "$" : (cur == "EUR" ? "€" : (cur + " "));
   return (neg ? "-" : "") + sym + out + frac;
}
//+------------------------------------------------------------------+
