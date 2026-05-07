//+------------------------------------------------------------------+
//|                                                    AntiGreed.mq5 |
//|        Draw entry/exit markers, labels, and SL/TP lines for the  |
//|        AntiGreed bot's trades on the active MT5 chart.           |
//+------------------------------------------------------------------+
//|                                                                  |
//|  Copy this file to:                                              |
//|     <MT5 data folder>\MQL5\Indicators\AntiGreed.mq5              |
//|  Open MetaEditor → File → Open → AntiGreed.mq5 → press F7 to     |
//|  compile. Then from MT5: drag the indicator onto any chart of    |
//|  a symbol the bot trades.                                        |
//|                                                                  |
//|  The bot stamps every order's comment with "AG · ..." — this     |
//|  indicator picks them out by that prefix and draws:              |
//|    ▲ green arrow + label at every BUY entry                      |
//|    ▼ red arrow + label at every SELL entry                       |
//|    ✓ green checkmark at winning closes (with PnL)                |
//|    ✗ red X at losing closes (with PnL)                           |
//|    dashed SL (red) and TP (green) lines on currently-open trades |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "AntiGreed"
#property link      "https://github.com/MartinDev69/Forex-EA"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

input long   InpMagic         = 0;        // Bot magic number (0 = match by comment prefix only)
input string InpCommentPrefix = "AG ·";   // Comment prefix that marks bot trades
input color  InpBuyColor      = clrLime;
input color  InpSellColor     = clrTomato;
input color  InpWinColor      = clrLime;
input color  InpLossColor     = clrTomato;
input int    InpRefreshSec    = 5;        // How often to scan for new trades
input bool   InpShowSlTp      = true;     // Draw SL/TP dashed lines on open trades
input bool   InpShowExits     = true;     // Draw exit markers for closed trades
input int    InpHistoryDays   = 7;        // How far back in history to scan for exits
input string InpObjPrefix     = "AG_";    // Object-name prefix (don't change unless conflicting)

//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(InpRefreshSec);
   RefreshChart();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectsDeleteAll(0, InpObjPrefix);
   ChartRedraw();
}

void OnTimer()
{
   RefreshChart();
}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   // Indicator doesn't compute price buffers — all drawing happens
   // off the timer / on demand. Return rates_total so MT5 considers
   // us "up to date".
   return rates_total;
}

//+------------------------------------------------------------------+
void RefreshChart()
{
   DrawOpenPositions();
   if(InpShowExits) DrawClosedExits();
   ChartRedraw();
}

bool IsBotTrade(const string comment, const long magic)
{
   if(InpMagic != 0 && magic == InpMagic) return true;
   return StringLen(InpCommentPrefix) > 0 && StringFind(comment, InpCommentPrefix) == 0;
}

// Pull the strategy short-code out of a comment like
// "AG · MAcross · B · trend" -> "MAcross". Falls back to the whole
// comment if the format doesn't match.
string ExtractStrategy(const string comment)
{
   string sep = " · ";
   int sl = StringLen(sep);
   int p1 = StringFind(comment, sep);
   if(p1 < 0) return comment;
   int p2 = StringFind(comment, sep, p1 + sl);
   if(p2 < 0) return StringSubstr(comment, p1 + sl);
   return StringSubstr(comment, p1 + sl, p2 - p1 - sl);
}

//+------------------------------------------------------------------+
void DrawOpenPositions()
{
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      string comment = PositionGetString(POSITION_COMMENT);
      long magic = (long)PositionGetInteger(POSITION_MAGIC);
      if(!IsBotTrade(comment, magic)) continue;

      double price = PositionGetDouble(POSITION_PRICE_OPEN);
      datetime t   = (datetime)PositionGetInteger(POSITION_TIME);
      bool isBuy   = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;

      // Entry arrow
      string arrow = InpObjPrefix + "entry_" + IntegerToString(ticket);
      if(ObjectFind(0, arrow) < 0)
      {
         ObjectCreate(0, arrow, OBJ_ARROW, 0, t, price);
         ObjectSetInteger(0, arrow, OBJPROP_ARROWCODE, isBuy ? 233 : 234);
         ObjectSetInteger(0, arrow, OBJPROP_COLOR, isBuy ? InpBuyColor : InpSellColor);
         ObjectSetInteger(0, arrow, OBJPROP_WIDTH, 3);
         ObjectSetInteger(0, arrow, OBJPROP_BACK, false);
      }

      // Strategy label next to the arrow
      string label = InpObjPrefix + "label_" + IntegerToString(ticket);
      if(ObjectFind(0, label) < 0)
      {
         string strat = ExtractStrategy(comment);
         string text = (isBuy ? "  AG " : "  AG ") + strat;
         ObjectCreate(0, label, OBJ_TEXT, 0, t, price);
         ObjectSetString(0, label, OBJPROP_TEXT, text);
         ObjectSetInteger(0, label, OBJPROP_COLOR, isBuy ? InpBuyColor : InpSellColor);
         ObjectSetInteger(0, label, OBJPROP_FONTSIZE, 9);
         ObjectSetInteger(0, label, OBJPROP_ANCHOR, isBuy ? ANCHOR_LOWER : ANCHOR_UPPER);
         ObjectSetString(0, label, OBJPROP_FONT, "Arial Bold");
      }

      // SL/TP dashed horizontal lines from entry forward
      if(InpShowSlTp)
      {
         double sl = PositionGetDouble(POSITION_SL);
         double tp = PositionGetDouble(POSITION_TP);
         if(sl > 0) DrawLevelLine(InpObjPrefix + "sl_" + IntegerToString(ticket),
                                  sl, InpSellColor, t);
         if(tp > 0) DrawLevelLine(InpObjPrefix + "tp_" + IntegerToString(ticket),
                                  tp, InpWinColor, t);
      }
   }
}

//+------------------------------------------------------------------+
void DrawClosedExits()
{
   datetime from = TimeCurrent() - InpHistoryDays * 24 * 3600;
   if(!HistorySelect(from, TimeCurrent())) return;

   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetString(deal, DEAL_SYMBOL) != _Symbol) continue;

      string comment = HistoryDealGetString(deal, DEAL_COMMENT);
      long magic = (long)HistoryDealGetInteger(deal, DEAL_MAGIC);
      if(!IsBotTrade(comment, magic)) continue;

      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_INOUT) continue;

      double price  = HistoryDealGetDouble(deal, DEAL_PRICE);
      datetime t    = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
      double profit = HistoryDealGetDouble(deal, DEAL_PROFIT)
                    + HistoryDealGetDouble(deal, DEAL_SWAP)
                    + HistoryDealGetDouble(deal, DEAL_COMMISSION);
      bool win = profit > 0;

      string name = InpObjPrefix + "exit_" + IntegerToString(deal);
      if(ObjectFind(0, name) < 0)
      {
         ObjectCreate(0, name, OBJ_ARROW, 0, t, price);
         ObjectSetInteger(0, name, OBJPROP_ARROWCODE, win ? 251 : 252);
         ObjectSetInteger(0, name, OBJPROP_COLOR, win ? InpWinColor : InpLossColor);
         ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, name, OBJPROP_BACK, false);
      }

      string label = InpObjPrefix + "exit_label_" + IntegerToString(deal);
      if(ObjectFind(0, label) < 0)
      {
         string sign = (profit >= 0) ? "+" : "";
         string text = sign + DoubleToString(profit, 2);
         ObjectCreate(0, label, OBJ_TEXT, 0, t, price);
         ObjectSetString(0, label, OBJPROP_TEXT, text);
         ObjectSetInteger(0, label, OBJPROP_COLOR, win ? InpWinColor : InpLossColor);
         ObjectSetInteger(0, label, OBJPROP_FONTSIZE, 8);
         ObjectSetString(0, label, OBJPROP_FONT, "Arial Bold");
      }
   }
}

//+------------------------------------------------------------------+
void DrawLevelLine(const string name, const double price, const color c, const datetime from)
{
   if(ObjectFind(0, name) < 0)
   {
      datetime to = TimeCurrent() + 24 * 3600;
      ObjectCreate(0, name, OBJ_TREND, 0, from, price, to, price);
      ObjectSetInteger(0, name, OBJPROP_COLOR, c);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DASH);
      ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
   }
}
//+------------------------------------------------------------------+
