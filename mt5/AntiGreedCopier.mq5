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
#property copyright "AntiGreed"
#property link      "https://github.com/MartinDev69/Forex-EA"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

input string  ApiBaseUrl        = "http://163.5.178.251:8000";  // from your dashboard
input string  ApiToken          = "";                            // ea_... key from dashboard
input string  AdId              = "";                            // your AD-ID (informational)
input int     PollSeconds       = 5;                             // how often to poll the feed
input double  RiskMultiplier    = 1.0;                           // scale every signal's lot
input double  MaxLotPerTrade    = 1.0;                           // hard cap
input string  SymbolSuffix      = "";                            // e.g. "m" if your broker uses EURUSDm
input bool    PlaceTakeProfit   = true;                          // mirror admin's TP
input bool    PlaceStopLoss     = true;                          // mirror admin's SL
input long    Magic             = 271828;                        // ours-vs-theirs filter
input bool    Verbose           = true;                          // chatty journal logging
input int     AccountReportSeconds = 60;                         // how often to push account snapshot

CTrade trade;

// Bookmark we send back as ?since= on the next poll. ISO-8601.
string g_bookmark = "";

// Wall clock of the last successful account snapshot POST.
datetime g_last_account_report = 0;

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
   Print("AntiGreedCopier started · base=", ApiBaseUrl,
         " · token=", StringSubstr(ApiToken, 0, 8), "..." ,
         " · bookmark=", (g_bookmark == "" ? "<none>" : g_bookmark));

   EventSetTimer(MathMax(2, PollSeconds));
   Poll();
   ReportAccount();  // first snapshot immediately so the dashboard lights up
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   Poll();
   MaybeReportAccount();
}

void OnTick()
{
   // Pure timer-driven — OnTick is a no-op.
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
      }
      else if(Verbose)
      {
         Print("AntiGreedCopier: WebRequest error ", err);
      }
      return;
   }
   if(code != 200)
   {
      Print("AntiGreedCopier: HTTP ", code, " from /signals/feed");
      return;
   }

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
//+------------------------------------------------------------------+
