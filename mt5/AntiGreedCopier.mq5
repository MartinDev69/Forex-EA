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
input string  EnabledSymbols    = "";                            // comma-separated whitelist (empty = all)
input bool    PlaceTakeProfit   = true;                          // mirror admin's TP
input bool    PlaceStopLoss     = true;                          // mirror admin's SL
input long    Magic             = 271828;                        // ours-vs-theirs filter
input bool    Verbose           = true;                          // chatty journal logging
input int     AccountReportSeconds = 60;                         // how often to push account snapshot

input group "═══ On-chart panel ═══"
input bool                ShowPanel         = true;              // render the status panel
input bool                PanelCentered     = true;              // auto-center on the chart (overrides offsets)
input int                 PanelWidth        = 420;               // panel width in px
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
// Layout constants — px from the panel's top-left corner.
#define P_PAD          16
#define P_HEADER_H     52
#define P_ROW_H        20
#define P_SECTION_GAP  12
#define P_FONT         "Consolas"
#define P_FONT_BODY    "Segoe UI"

// Panel width (live; pulled from PanelWidth input on init) and its
// computed top-left position in chart pixels. Refreshed whenever the
// chart resizes via OnChartEvent.
int g_panel_w = 420;
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
int PanelHeight()
{
   int symbol_rows = MathMax(1, (int)MathCeil((g_filter_active ? g_enabled_count : 1) / 4.0));
   int chip_h = 22;
   // KPI tile row (64h) + last-copy line + 2 dividers (with surrounding
   // gaps) + symbol-header row + symbol-chip rows + 3 account rows + pad.
   int height = P_HEADER_H
              + P_SECTION_GAP            // gap below header
              + 64                       // tile row
              + P_SECTION_GAP
              + P_ROW_H                  // last-copy detail line
              + P_SECTION_GAP * 2 + 1    // div1 + gaps
              + P_ROW_H + 2              // SYMBOLS heading
              + symbol_rows * (chip_h + 4)
              + P_SECTION_GAP * 2 + 1    // div2 + gaps
              + 3 * P_ROW_H              // balance, equity, risk
              + P_PAD;
   return height;
}

// Recompute where the panel's top-left should sit on the chart, based
// on PanelCentered + PanelWidth + nudge offsets. Called on init and
// whenever the chart resizes.
void LayoutPanel()
{
   g_panel_w = MathMax(280, PanelWidth);
   int h = PanelHeight();
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

// One-time build — everything else is a redraw of text values.
void BuildPanel()
{
   ObjectsDeleteAll(0, PNL);
   LayoutPanel();
   int h = PanelHeight();
   int x = g_panel_x;
   int y = g_panel_y;

   // ── Backdrop: full-panel filled box (chart can't bleed through). ──
   MakeBox(PNL + "card", x, y, g_panel_w, h,
           PanelBgColor, PanelBorderColor);

   // ── Header strip: subtle gradient feel via a second slightly-lighter
   //    band, plus a neon accent strip on the very left edge. ──────────
   MakeBox(PNL + "hdr", x, y, g_panel_w, P_HEADER_H,
           C'18,24,38', PanelBorderColor);
   // Left vertical neon bar — bright brand stripe.
   MakeBox(PNL + "bar", x, y, 6, P_HEADER_H + 2,
           PanelAccentColor, PanelAccentColor);
   // Thin underline beneath the header to separate it from the body.
   MakeBox(PNL + "uline", x, y + P_HEADER_H, g_panel_w, 2,
           PanelAccentColor, PanelAccentColor);

   // ── "AG" badge — replaces the unreliable BMP with a solid colored
   //    box and bold letterform. ──────────────────────────────────────
   MakeBox(PNL + "badge", x + 16, y + 10, 34, 32,
           PanelAccentColor, PanelAccentColor);
   MakeLabel(PNL + "badge_t", x + 33, y + 14, "AG",
             C'8,18,12', 14, "Segoe UI Black", ANCHOR_UPPER);

   // ── Title + tagline ───────────────────────────────────────────────
   MakeLabel(PNL + "title", x + 62, y + 11,
             "ANTIGREED  COPIER", PanelTextColor, 13, "Segoe UI Semibold");
   MakeLabel(PNL + "sub",   x + 62, y + 31,
             "live signal mirror", PanelMutedColor, 8, P_FONT_BODY);

   // ── Status pill on the right side of the header ──────────────────
   int pill_w = 96;
   MakeBox(PNL + "pill", x + g_panel_w - pill_w - 14, y + 14, pill_w, 24,
           C'30,44,28', PanelAccentColor);
   MakeLabel(PNL + "dot",   x + g_panel_w - pill_w - 4, y + 19,
             "●", PanelAccentColor, 11, P_FONT_BODY);
   MakeLabel(PNL + "pill_t", x + g_panel_w - pill_w + 14, y + 17,
             "LIVE", PanelAccentColor, 9, "Segoe UI Semibold");
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
   int y = g_panel_y + P_HEADER_H + P_SECTION_GAP;

   // Status colors derive from g_last_status — used for the pill and dot.
   color st_clr = PanelAccentColor;
   color st_bg  = C'30,44,28';
   string st_text = "LIVE";
   if(g_last_status == "blocked")
   {
      st_clr = C'255,179,0'; st_bg = C'52,38,8'; st_text = "BLOCKED";
   }
   else if(g_last_status == "stopped")
   {
      st_clr = PanelDangerColor; st_bg = C'58,18,28'; st_text = "STOPPED";
   }
   ObjectSetInteger(0, PNL + "dot",    OBJPROP_COLOR, st_clr);
   ObjectSetInteger(0, PNL + "pill",   OBJPROP_BGCOLOR, st_bg);
   ObjectSetInteger(0, PNL + "pill",   OBJPROP_BORDER_COLOR, st_clr);
   ObjectSetInteger(0, PNL + "pill_t", OBJPROP_COLOR, st_clr);
   ObjectSetString (0, PNL + "pill_t", OBJPROP_TEXT, st_text);
   // Tagline reflects health.
   string sub = "live signal mirror · poll " +
                IntegerToString(MathMax(2, PollSeconds)) + "s";
   if(g_last_status != "live" && StringLen(g_last_error) > 0) sub = g_last_error;
   ObjectSetString(0, PNL + "sub", OBJPROP_TEXT, sub);

   // ─── KPI tile row: three side-by-side metric cards. ──────────────
   int gap = 8;
   int tile_w = (g_panel_w - (P_PAD * 2) - (gap * 2)) / 3;
   int tile_h = 64;
   DrawTile("t_today", x + P_PAD,                    y, tile_w, tile_h,
            "COPIED TODAY", IntegerToString(g_copies_today), PanelAccentColor);
   DrawTile("t_open",  x + P_PAD + tile_w + gap,     y, tile_w, tile_h,
            "OPEN POSITIONS", IntegerToString(OurOpenPositions()), PanelAccentColor);
   DrawTile("t_last",  x + P_PAD + (tile_w + gap)*2, y, tile_w, tile_h,
            "LAST COPY",
            (g_last_copy_time > 0 ? ShortTime(g_last_copy_time) : "—"),
            PanelTextColor);
   y += tile_h + P_SECTION_GAP;

   // Sub-row beneath the last-copy tile showing the actual trade text.
   string last = (g_last_copy_text == "—") ? "no copies yet" : g_last_copy_text;
   MakeLabel(PNL + "last_full", x + P_PAD, y, "↗  " + last,
             PanelMutedColor, 9, P_FONT_BODY);
   y += P_ROW_H;

   // ─── Section divider ─────────────────────────────────────────────
   DrawDivider("div1", x, y);
   y += P_SECTION_GAP;

   // ─── Symbol whitelist chips ──────────────────────────────────────
   string syms_hdr = g_filter_active
      ? "SYMBOLS  ·  " + IntegerToString(g_enabled_count) + " enabled"
      : "SYMBOLS  ·  all admin signals";
   MakeLabel(PNL + "syms_hdr", x + P_PAD, y, syms_hdr, PanelMutedColor, 9, P_FONT_BODY);
   y += P_ROW_H + 2;
   ClearSymbolChips();
   if(g_filter_active)
   {
      int per_row = 4, chip_w = (g_panel_w - (P_PAD * 2) - 18) / per_row;
      int chip_h = 22;
      for(int i = 0; i < g_enabled_count; i++)
      {
         int col = i % per_row;
         int row = i / per_row;
         int cx = x + P_PAD + col * (chip_w + 6);
         int cy = y + row * (chip_h + 4);
         string nm = PNL + "chip_" + IntegerToString(i);
         MakeBox(nm + "_bg", cx, cy, chip_w, chip_h,
                 C'22,32,48', PanelBorderColor,
                 g_enabled_symbols[i], PanelAccentColor, 9,
                 "Segoe UI Semibold", ALIGN_CENTER);
      }
      int rows = (int)MathCeil(g_enabled_count / 4.0);
      y += rows * (chip_h + 4);
   }
   else
   {
      MakeLabel(PNL + "syms_all", x + P_PAD, y,
                "set EnabledSymbols=EURUSD,GBPUSD,... to filter",
                PanelMutedColor, 8, P_FONT_BODY);
      y += P_ROW_H;
   }
   y += P_SECTION_GAP;

   // ─── Section divider ─────────────────────────────────────────────
   DrawDivider("div2", x, y);
   y += P_SECTION_GAP;

   // ─── Account snapshot ────────────────────────────────────────────
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double floating = eq - bal;
   color eq_clr = (floating >= 0) ? PanelAccentColor : PanelDangerColor;
   DrawKv("k_bal",  y, "BALANCE", FmtMoney(bal, cur), PanelTextColor);
   y += P_ROW_H;
   DrawKv("k_eq",   y, "EQUITY",  FmtMoney(eq,  cur), eq_clr);
   y += P_ROW_H;
   string risk_line = StringFormat("x %.2f  ·  max %.2f lot",
                                   RiskMultiplier, MaxLotPerTrade);
   DrawKv("k_risk", y, "RISK",    risk_line, PanelMutedColor);

   ChartRedraw();
}

void DrawTile(const string id, int xoff, int yoff, int w, int h,
              const string label, const string value, color val_clr)
{
   MakeBox(PNL + id + "_bg",  xoff, yoff, w, h,
           C'14,20,32', PanelBorderColor);
   // Top accent strip on each tile for that "live ticker" feel.
   MakeBox(PNL + id + "_top", xoff, yoff, w, 3,
           val_clr, val_clr);
   MakeLabel(PNL + id + "_l", xoff + 10, yoff + 9,
             label, PanelMutedColor, 8, P_FONT_BODY);
   MakeLabel(PNL + id + "_v", xoff + 10, yoff + 26,
             value, val_clr, 18, "Segoe UI Semibold");
}

void DrawDivider(const string id, int x_anchor, int y)
{
   // Subtle horizontal line that sits inset from the panel edges.
   MakeBox(PNL + id, x_anchor + P_PAD, y, g_panel_w - (P_PAD * 2), 1,
           C'40,52,72', C'40,52,72');
}

void DrawKv(const string id, int y, const string label, const string value, color val_clr)
{
   int x = g_panel_x;
   MakeLabel(PNL + id + "_l", x + P_PAD,             y, label, PanelMutedColor, 9, P_FONT_BODY);
   MakeLabel(PNL + id + "_v", x + g_panel_w - P_PAD, y, value, val_clr,         11, "Segoe UI Semibold", ANCHOR_RIGHT_UPPER);
}

void ClearSymbolChips()
{
   for(int i = 0; i < 50; i++)
   {
      ObjectDelete(0, PNL + "chip_" + IntegerToString(i));
      ObjectDelete(0, PNL + "chip_" + IntegerToString(i) + "_bg");
   }
   ObjectDelete(0, PNL + "syms_all");
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
