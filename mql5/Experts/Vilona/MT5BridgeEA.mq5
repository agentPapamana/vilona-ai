//+------------------------------------------------------------------+
//| MT5BridgeEA.mq5 - Execution & Data Bridge for Vilona AI           |
//| Place in MQL5/Experts/Vilona/. Attach to chart or run via startup |
//+------------------------------------------------------------------+
#property copyright "vilona-ai"
#property version   "1.20"
#property strict

#include <Trade\Trade.mqh>

input string AgentURL   = "http://127.0.0.1:8787";                         // Agent base URL
input string AgentToken = "TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE";   // Agent auth token
input int    PollMs     = 500;                                             // Poll interval in milliseconds
input bool   Verbose    = true;                                            // Verbose logging to Experts log

CTrade   trade;
string   g_headers;
datetime g_lastPoll = 0;

int OnInit()
  {
   g_headers = "Content-Type: application/json\r\nX-MT5-Agent-Token: " + AgentToken + "\r\n";
   EventSetMillisecondTimer(PollMs);
   Print("MT5BridgeEA v1.20 started. Terminal build=", TerminalInfoInteger(TERMINAL_BUILD),
         " trade_allowed=", TerminalInfoInteger(TERMINAL_TRADE_ALLOWED),
         " connected=", TerminalInfoInteger(TERMINAL_CONNECTED));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Print("MT5BridgeEA stopped. reason=", reason);
  }

string HttpGet(const string url)
  {
   char data[], result[];
   string rh;
   ResetLastError();
   int code = WebRequest("GET", url, g_headers, 5000, data, result, rh);
   if(code == -1)
     {
      int err = GetLastError();
      if(err == 4060)
         Print("CRITICAL: WebRequest URL not allowed! Add '", AgentURL, "' in Tools -> Options -> Expert Advisors");
      return("{\"success\":false,\"error\":\"webrequest_failed_" + IntegerToString(err) + "\"}");
     }
   if(code != 200) return("{\"success\":false,\"error\":\"http_" + IntegerToString(code) + "\"}");
   return(CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
  }

string HttpPost(const string url, const string payload)
  {
   char data[], result[];
   string rh;
   StringToCharArray(payload, data, 0, StringLen(payload), CP_UTF8);
   ResetLastError();
   int code = WebRequest("POST", url, g_headers, 5000, data, result, rh);
   if(code == -1)
     {
      int err = GetLastError();
      if(err == 4060)
         Print("CRITICAL: WebRequest URL not allowed! Add '", AgentURL, "' in Tools -> Options -> Expert Advisors");
      return("{\"success\":false,\"error\":\"webrequest_failed_" + IntegerToString(err) + "\"}");
     }
   if(code != 200) return("{\"success\":false,\"error\":\"http_" + IntegerToString(code) + "\"}");
   return(CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
  }

//--- JSON string field extractor
string JsonStr(const string src, const string key)
  {
   string pat = "\"" + key + "\":\"";
   int p = StringFind(src, pat);
   if(p < 0) return("");
   p += StringLen(pat);
   int e = StringFind(src, "\"", p);
   if(e < 0) return("");
   return(StringSubstr(src, p, e - p));
  }

//--- JSON numeric field extractor
string JsonNum(const string src, const string key)
  {
   string pat = "\"" + key + "\":";
   int p = StringFind(src, pat);
   if(p < 0) return("");
   p += StringLen(pat);
   int e = p;
   int len = StringLen(src);
   while(e < len)
     {
      ushort c = StringGetCharacter(src, e);
      if(c == ',' || c == '}') break;
      e++;
     }
   return(StringSubstr(src, p, e - p));
  }

string DocAccount()
  {
   string j = "{\"success\":true,\"data\":{";
   j += "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   j += "\"server\":\"" + AccountInfoString(ACCOUNT_SERVER) + "\",";
   j += "\"currency\":\"" + AccountInfoString(ACCOUNT_CURRENCY) + "\",";
   j += "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   j += "\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   j += "\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
   j += "\"free_margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
   j += "\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false") + ",";
   j += "\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") + ",";
   j += "\"build\":" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD));
   j += "}}";
   return(j);
  }

int SymDigits(const string symbol)
  {
   int d = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(d < 0 || d > 8) d = _Digits;
   return(d);
  }

string DocTick(const string symbol)
  {
   MqlTick t;
   if(!SymbolInfoTick(symbol, t))
      return("{\"success\":false,\"error\":\"tick_unavailable\",\"symbol\":\"" + symbol + "\"}");
   long server_offset_s = (long)(TimeCurrent() - TimeGMT());
   long utc_time = (long)t.time - server_offset_s;
   long utc_time_msc = (long)t.time_msc - (server_offset_s * 1000);
   string j = "{\"success\":true,\"data\":{";
   j += "\"symbol\":\"" + symbol + "\",";
   int dg = SymDigits(symbol);
   j += "\"bid\":" + DoubleToString(t.bid, dg) + ",";
   j += "\"ask\":" + DoubleToString(t.ask, dg) + ",";
   j += "\"last\":" + DoubleToString(t.last, dg) + ",";
   j += "\"volume\":" + IntegerToString((long)t.volume) + ",";
   j += "\"time\":" + IntegerToString(utc_time) + ",";
   j += "\"time_msc\":" + IntegerToString(utc_time_msc) + ",";
   j += "\"server_offset_s\":" + IntegerToString(server_offset_s);
   j += "}}";
   return(j);
  }

string DocPositions(const string symbolFilter)
  {
   int total = PositionsTotal();
   string j = "{\"success\":true,\"data\":{\"count\":0,\"positions\":[";
   bool first = true;
   int cnt = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(symbolFilter != "" && sym != symbolFilter) continue;
      if(!first) j += ",";
      first = false;
      cnt++;
      j += "{";
      j += "\"ticket\":" + IntegerToString((long)ticket) + ",";
      j += "\"symbol\":\"" + sym + "\",";
      j += "\"type\":" + IntegerToString((int)PositionGetInteger(POSITION_TYPE)) + ",";
      j += "\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + ",";
      int pdg = SymDigits(sym);
      j += "\"price_open\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), pdg) + ",";
      j += "\"price_current\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), pdg) + ",";
      j += "\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), pdg) + ",";
      j += "\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), pdg) + ",";
      j += "\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ",";
      j += "\"magic\":" + IntegerToString((long)PositionGetInteger(POSITION_MAGIC));
      j += "}";
     }
   j += "],\"count\":" + IntegerToString(cnt) + "}}";
   return(j);
  }

string DocPendingOrders(const string symbolFilter)
  {
   int total = OrdersTotal();
   string j = "{\"success\":true,\"data\":{\"count\":0,\"orders\":[";
   bool first = true;
   int cnt = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      if(symbolFilter != "" && sym != symbolFilter) continue;
      if(!first) j += ",";
      first = false;
      cnt++;
      j += "{";
      j += "\"ticket\":" + IntegerToString((long)ticket) + ",";
      j += "\"symbol\":\"" + sym + "\",";
      j += "\"type\":" + IntegerToString((int)OrderGetInteger(ORDER_TYPE)) + ",";
      j += "\"volume\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 2) + ",";
      int odg = SymDigits(sym);
      j += "\"price_open\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), odg) + ",";
      j += "\"sl\":" + DoubleToString(OrderGetDouble(ORDER_SL), odg) + ",";
      j += "\"tp\":" + DoubleToString(OrderGetDouble(ORDER_TP), odg) + ",";
      j += "\"time_setup\":" + IntegerToString((long)OrderGetInteger(ORDER_TIME_SETUP));
      j += "}";
     }
   j += "],\"count\":" + IntegerToString(cnt) + "}}";
   return(j);
  }

string DocHistoryDeals(const string args)
  {
   string sinceText = JsonNum(args, "since");
   if(sinceText == "")
      return("{\"success\":false,\"error\":\"since_required\"}");
   long sinceEpoch = StringToInteger(sinceText);
   datetime now = TimeCurrent();
   if(sinceEpoch <= 0 || sinceEpoch > (long)now)
      return("{\"success\":false,\"error\":\"invalid_since\"}");
   if(!HistorySelect((datetime)sinceEpoch, now))
      return("{\"success\":false,\"error\":\"history_select_failed\"}");
   int total = HistoryDealsTotal();
   string j = "{\"success\":true,\"data\":{\"deals\":[";
   bool first = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      datetime dealTime = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      long positionId = HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
      long magic = HistoryDealGetInteger(ticket, DEAL_MAGIC);
      double price = HistoryDealGetDouble(ticket, DEAL_PRICE);
      double volume = HistoryDealGetDouble(ticket, DEAL_VOLUME);
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT);
      double commission = HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      double swap = HistoryDealGetDouble(ticket, DEAL_SWAP);
      double fee = HistoryDealGetDouble(ticket, DEAL_FEE);
      if(!first) j += ",";
      first = false;
      j += "{\"ticket\":" + IntegerToString((long)ticket) + ",";
      j += "\"position_id\":" + IntegerToString(positionId) + ",";
      j += "\"magic\":" + IntegerToString(magic) + ",";
      j += "\"price\":" + DoubleToString(price, SymDigits(symbol)) + ",";
      j += "\"volume\":" + DoubleToString(volume, 2) + ",";
      j += "\"symbol\":\"" + symbol + "\",\"time\":" + IntegerToString((long)dealTime) + ",";
      j += "\"entry\":\"" + (entry == DEAL_ENTRY_OUT ? "out" : "out_by") + "\",";
      j += "\"profit\":" + DoubleToString(profit, 2) + ",";
      j += "\"commission\":" + DoubleToString(commission, 2) + ",";
      j += "\"swap\":" + DoubleToString(swap, 2) + ",";
      j += "\"fee\":" + DoubleToString(fee, 2) + "}";
     }
   j += "]}}";
   return(j);
  }

string DocServerDayStart()
  {
   MqlDateTime day;
   if(!TimeToStruct(TimeCurrent(), day))
      return("{\"success\":false,\"error\":\"server_time_unavailable\"}");
   day.hour = 0;
   day.min = 0;
   day.sec = 0;
   datetime boundary = StructToTime(day);
   if(boundary <= 0)
      return("{\"success\":false,\"error\":\"server_day_boundary_unavailable\"}");
   return("{\"success\":true,\"data\":{\"server_day_start\":" + IntegerToString((long)boundary) + "}}");
  }

string DocDailyPL(const string dateText)
  {
   datetime now = TimeCurrent();
   MqlDateTime day; TimeToStruct(now, day);
   day.hour = 0; day.min = 0; day.sec = 0;
   datetime from = StructToTime(day);
   if(dateText != "")
     {
      datetime parsed = StringToTime(dateText);
      if(parsed > 0) from = parsed;
     }
   if(!HistorySelect(from, now))
      return("{\"success\":false,\"error\":\"history_select_failed\"}");
   double total = 0.0;
   int deals = HistoryDealsTotal();
   for(int i = 0; i < deals; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      total += HistoryDealGetDouble(ticket, DEAL_PROFIT);
      total += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      total += HistoryDealGetDouble(ticket, DEAL_SWAP);
      total += HistoryDealGetDouble(ticket, DEAL_FEE);
     }
   return("{\"success\":true,\"data\":{\"date\":\"" + TimeToString(from, TIME_DATE) + "\",\"realized_pl\":" + DoubleToString(total, 2) + ",\"deals\":" + IntegerToString(deals) + "}}");
  }

//--- Timeframe name -> period. Shared by the tail and the range reader so the
//--- two entry points can never disagree about what "M15" means.
ENUM_TIMEFRAMES ParsePeriod(const string tf)
  {
   if(tf == "M5")  return(PERIOD_M5);
   if(tf == "M15") return(PERIOD_M15);
   if(tf == "M30") return(PERIOD_M30);
   if(tf == "H1")  return(PERIOD_H1);
   if(tf == "H4")  return(PERIOD_H4);
   if(tf == "D1")  return(PERIOD_D1);
   return(PERIOD_M1);
  }

string RatesToJson(const string symbol, const string tf, const int n, const MqlRates &r[])
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits < 0 || digits > 8) digits = _Digits;
   string j = "{\"success\":true,\"data\":{\"symbol\":\"" + symbol + "\",\"timeframe\":\"" + tf + "\",\"count\":" + IntegerToString(n) + ",\"rates\":[";
   for(int i = 0; i < n; i++)
     {
      if(i > 0) j += ",";
      j += "{\"time\":" + IntegerToString((long)r[i].time);
      j += ",\"open\":" + DoubleToString(r[i].open, digits);
      j += ",\"high\":" + DoubleToString(r[i].high, digits);
      j += ",\"low\":" + DoubleToString(r[i].low, digits);
      j += ",\"close\":" + DoubleToString(r[i].close, digits);
      j += ",\"tick_volume\":" + IntegerToString((long)r[i].tick_volume) + "}";
     }
   j += "]}}";
   return(j);
  }

string DocCandles(const string symbol, const string tf, const int count)
  {
   MqlRates r[];
   int n = CopyRates(symbol, ParsePeriod(tf), 0, count, r);
   if(n <= 0) return("{\"success\":false,\"error\":\"rates_unavailable\",\"symbol\":\"" + symbol + "\"}");
   return(RatesToJson(symbol, tf, n, r));
  }

//--- Historical window reader for the backtest gate (B9). The live tail is
//--- capped at 500 bars, which is ~7 days on M15; six months needs a dated
//--- CopyRates call instead. `from`/`to` are epoch seconds. The result is
//--- chronological (oldest first) so a paginator can walk backwards from `to`.
//--- The per-call ceiling keeps the JSON string inside MQL5's buffer limits;
//--- callers paginate rather than asking for a multi-megabyte response.
#define MAX_RANGE_BARS 5000

string DocCandlesRange(const string symbol, const string tf, const long from, const long to, const int limit)
  {
   if(from <= 0 || to <= 0 || from >= to)
      return("{\"success\":false,\"error\":\"invalid_range\"}");
   ENUM_TIMEFRAMES period = ParsePeriod(tf);
   int want = limit;
   if(want <= 0 || want > MAX_RANGE_BARS) want = MAX_RANGE_BARS;
   // Widen the requested window by the number of bars the caller asked for so a
   // paginator that steps back by `limit` bars per call still receives a full
   // page even when the terminal holds gaps (weekends, holidays).
   long span = (long)PeriodSeconds(period) * (long)(want + 1);
   datetime start = (datetime)(from - span < 0 ? 0 : from - span);
   MqlRates r[];
   int n = CopyRates(symbol, period, start, (datetime)to, r);
   if(n <= 0)
      return("{\"success\":false,\"error\":\"rates_unavailable\",\"symbol\":\"" + symbol + "\",\"from\":" + IntegerToString(from) + ",\"to\":" + IntegerToString(to) + "}");
   // Drop bars older than `from` and keep the tail so pagination is exact.
   int first = 0;
   while(first < n && (long)r[first].time < from) first++;
   int count = n - first;
   if(count > want) { first += count - want; count = want; }
   if(count <= 0)
      return("{\"success\":true,\"data\":{\"symbol\":\"" + symbol + "\",\"timeframe\":\"" + tf + "\",\"count\":0,\"rates\":[]}}");
   // CopyRates returns oldest-first; slice into a compact array for the encoder.
   MqlRates page[];
   ArrayResize(page, count);
   for(int i = 0; i < count; i++) page[i] = r[first + i];
   return(RatesToJson(symbol, tf, count, page));
  }

string ExecMarketOrder(const string args)
  {
   string symbol = JsonStr(args, "symbol");
   if(symbol == "") symbol = _Symbol;
   string action = JsonStr(args, "action");
   string vs = JsonNum(args, "volume");
   double volume = (vs == "" ? 0.01 : StringToDouble(vs));
   string ss = JsonNum(args, "sl");
   double sl = (ss == "" ? 0.0 : StringToDouble(ss));
   string ts = JsonNum(args, "tp");
   double tp = (ts == "" ? 0.0 : StringToDouble(ts));
   string comment = JsonStr(args, "comment");
   if(comment == "") comment = "vilona-ai";
   ulong magic = (ulong)StringToInteger(JsonNum(args, "magic"));
   if(magic > 0) trade.SetExpertMagicNumber(magic);

   trade.SetDeviationInPoints(20);
   bool ok = false;
   if(action == "buy" || action == "BUY")
      ok = trade.Buy(volume, symbol, 0, sl, tp, comment);
   else if(action == "sell" || action == "SELL")
      ok = trade.Sell(volume, symbol, 0, sl, tp, comment);
   else
      return("{\"success\":false,\"error\":\"invalid_action\",\"action\":\"" + action + "\"}");

   if(!ok)
      return("{\"success\":false,\"error\":\"order_failed\",\"retcode\":" + IntegerToString(trade.ResultRetcode()) + ",\"desc\":\"" + trade.ResultRetcodeDescription() + "\"}");

   ulong ticket = trade.ResultOrder();
   return("{\"success\":true,\"data\":{\"ticket\":" + IntegerToString((long)ticket) + ",\"volume\":" + DoubleToString(volume, 2) + ",\"symbol\":\"" + symbol + "\",\"action\":\"" + action + "\"}}");
  }

string DocSymbolInfo(const string symbol);
string DocModifyPosition(const string args);
string DocClosePosition(const string args);
string DocPendingOrders(const string symbolFilter);
string DocHistoryDeals(const string args);
string DocServerDayStart();

string ExecCommand(const string name, const string args)
  {
   if(name == "ping")
      return("{\"success\":true,\"data\":{\"pong\":true,\"build\":" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) + "}}");
   if(name == "symbol_limits")
     {
      string lm = JsonStr(args, "symbol");
      if(lm == "") return("{\"success\":false,\"error\":\"symbol_required\"}");
      if(!SymbolSelect(lm, true))
         return("{\"success\":false,\"error\":\"symbol_select_failed\"}");
      int stops = (int)SymbolInfoInteger(lm, SYMBOL_TRADE_STOPS_LEVEL);
      int freeze = (int)SymbolInfoInteger(lm, SYMBOL_TRADE_FREEZE_LEVEL);
      int spread = (int)SymbolInfoInteger(lm, SYMBOL_SPREAD);
      double lpoint = SymbolInfoDouble(lm, SYMBOL_POINT);
      string j = "{\"success\":true,\"data\":{";
      j += "\"symbol\":\"" + lm + "\",";
      j += "\"stops_level_points\":" + IntegerToString(stops) + ",";
      j += "\"freeze_level_points\":" + IntegerToString(freeze) + ",";
      j += "\"spread_points\":" + IntegerToString(spread) + ",";
      j += "\"point\":" + DoubleToString(lpoint, 6) + ",";
      j += "\"stops_level_price\":" + DoubleToString(stops * lpoint, 6);
      j += "}}";
      return(j);
     }
   if(name == "account_info") return(DocAccount());
   if(name == "get_tick")
     {
      string s = JsonStr(args, "symbol");
      if(s == "") s = _Symbol;
      return(DocTick(s));
     }
   if(name == "get_positions") return(DocPositions(JsonStr(args, "symbol")));
   if(name == "get_pending_orders") return(DocPendingOrders(JsonStr(args, "symbol")));
   if(name == "calculate_daily_pl") return(DocDailyPL(JsonStr(args, "date")));
   if(name == "get_server_day_start") return(DocServerDayStart());
   if(name == "get_history_deals") return(DocHistoryDeals(args));
   if(name == "get_candles")
     {
      string s = JsonStr(args, "symbol"); if(s == "") s = _Symbol;
      string tf = JsonStr(args, "timeframe"); if(tf == "") tf = "M1";
      string cs = JsonNum(args, "count"); int c = (cs == "" ? 10 : (int)StringToInteger(cs));
      if(c > 500) c = 500;
      return(DocCandles(s, tf, c));
     }
   if(name == "get_candles_range")
     {
      string s = JsonStr(args, "symbol"); if(s == "") s = _Symbol;
      string tf = JsonStr(args, "timeframe"); if(tf == "") tf = "M15";
      long from = StringToInteger(JsonNum(args, "from"));
      long to = StringToInteger(JsonNum(args, "to"));
      string ls = JsonNum(args, "limit");
      int limit = (ls == "" ? 1000 : (int)StringToInteger(ls));
      return(DocCandlesRange(s, tf, from, to, limit));
     }
   if(name == "get_symbol_info")
     {
      string s = JsonStr(args, "symbol"); if(s == "") s = _Symbol;
      return(DocSymbolInfo(s));
     }
   if(name == "send_market_order")
      return(ExecMarketOrder(args));
   if(name == "modify_position_sltp")
      return(DocModifyPosition(args));
   if(name == "close_position")
      return(DocClosePosition(args));

   return("{\"success\":false,\"error\":\"unsupported_command\",\"command\":\"" + name + "\"}");
  }

void OnTimer()
  {
   string resp = HttpGet(AgentURL + "/poll");
   if(StringFind(resp, "\"command\":null") >= 0) return;
   string id  = JsonStr(resp, "id");
   string cmd = JsonStr(resp, "command");
   if(id == "" || cmd == "") return;
   if(Verbose) Print("executing ", cmd, " id=", id);
   string out = ExecCommand(cmd, resp);
   if(Verbose) Print("result ", cmd, " -> ", StringSubstr(out, 0, 200));
   HttpPost(AgentURL + "/result/" + id, out);
  }

string DocSymbolInfo(const string symbol)
  {
   if(!SymbolSelect(symbol, true))
      return("{\"success\":false,\"error\":\"symbol_select_failed\",\"symbol\":\"" + symbol + "\"}");

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double lot_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double lot_min = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double lot_max = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);

   string j = "{\"success\":true,\"data\":{";
   j += "\"symbol\":\"" + symbol + "\",";
   j += "\"point\":" + DoubleToString(point, 6) + ",";
   j += "\"digits\":" + IntegerToString(digits) + ",";
   j += "\"lot_step\":" + DoubleToString(lot_step, 6) + ",";
   j += "\"lot_min\":" + DoubleToString(lot_min, 6) + ",";
   j += "\"lot_max\":" + DoubleToString(lot_max, 6) + ",";
   j += "\"tick_value\":" + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE), 6) + ",";
   j += "\"tick_size\":" + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE), 6) + ",";
   j += "\"position_type_buy\":\"buy\",";
   j += "\"position_type_sell\":\"sell\",";
   j += "\"order_type_buy_stop\":\"buy_stop\",";
   j += "\"order_type_buy_limit\":\"buy_limit\",";
   j += "\"order_type_sell_stop\":\"sell_stop\",";
   j += "\"order_type_sell_limit\":\"sell_limit\",";
   j += "\"deal_entry_out\":\"out\"";
   j += "}}";
   return(j);
  }


string DocModifyPosition(const string args)
  {
   string symbol = JsonStr(args, "symbol");
   if(symbol == "") symbol = _Symbol;
   ulong ticket = (ulong)StringToInteger(JsonNum(args, "ticket"));
   double sl = StringToDouble(JsonNum(args, "sl"));
   double tp = StringToDouble(JsonNum(args, "tp"));

   // Check position exists and is open
   if(!PositionSelectByTicket(ticket))
      return("{\"success\":false,\"error\":\"position_not_found\",\"ticket\":\"" + IntegerToString((long)ticket) + "\"}");

   // Modify SL/TP
   bool ok = trade.PositionModify(ticket, sl, tp);
   if(!ok)
      return("{\"success\":false,\"error\":\"position_modify_failed\",\"retcode\":" + IntegerToString(trade.ResultRetcode()) + ",\"desc\":\"" + trade.ResultRetcodeDescription() + "\"}");

   int mdg = SymDigits(symbol);
   return("{\"success\":true,\"data\":{\"ticket\":" + IntegerToString((long)ticket) + ",\"sl\":" + DoubleToString(sl, mdg) + ",\"tp\":" + DoubleToString(tp, mdg) + "}}");
  }

string DocClosePosition(const string args)
  {
   string symbol = JsonStr(args, "symbol");
   if(symbol == "") symbol = _Symbol;
   ulong ticket = (ulong)StringToInteger(JsonNum(args, "ticket"));
   double volume = StringToDouble(JsonNum(args, "volume"));
   string comment = JsonStr(args, "comment");
   if(comment == "") comment = "vilona-auto-close";
   ulong magic = (ulong)StringToInteger(JsonNum(args, "magic"));

   // Check position exists and is open
   if(!PositionSelectByTicket(ticket))
      return("{\"success\":false,\"error\":\"position_not_found\",\"ticket\":\"" + IntegerToString((long)ticket) + "\"}");

   // Close position
   bool ok = trade.PositionClose(ticket);
   if(!ok)
      return("{\"success\":false,\"error\":\"position_close_failed\",\"retcode\":" + IntegerToString(trade.ResultRetcode()) + ",\"desc\":\"" + trade.ResultRetcodeDescription() + "\"}");

   return("{\"success\":true,\"data\":{\"ticket\":" + IntegerToString((long)ticket) + ",\"symbol\":\"" + symbol + "\",\"comment\":\"" + comment + "\"}}");
  }

