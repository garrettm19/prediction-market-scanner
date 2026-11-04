export default function ModelPage() {
  const s = { color: "#94a3b8" };
  const h = "font-mono text-[13px] font-bold tracking-wide uppercase mt-8 mb-3";
  const p = "font-mono text-[12px] leading-relaxed mb-3";
  const li = "font-mono text-[12px] leading-relaxed mb-1.5";
  const accent = { color: "#2dd4bf" };
  const warn = { color: "#f59e0b" };
  const dim = { color: "#4b5563" };

  return (
    <main className="max-w-[720px] mx-auto px-6 py-8">
      <h1 className="font-mono text-[16px] font-bold tracking-[0.15em] uppercase mb-2" style={accent}>
        How the Model Works
      </h1>
      <p className={p} style={dim}>
        A betting-focused explanation of the EV scanner.
      </p>

      {/* 1 */}
      <h2 className={h} style={accent}>1. What this tool does</h2>
      <p className={p} style={s}>
        This tool finds mispriced bets on prediction markets. It compares prices on
        Polymarket and Kalshi against FanDuel sportsbook odds to identify when you can
        buy an outcome for less than it&apos;s worth.
      </p>
      <p className={p} style={s}>
        It does not predict who will win. It compares prices &mdash; like finding a $10
        item selling for $7 at a different store.
      </p>
      <p className={p} style={s}>
        FanDuel odds are the reference because they reflect real money from sharp bettors
        and professional bookmakers. When a prediction market prices something cheaper than
        what FanDuel implies, that&apos;s a potential opportunity.
      </p>

      {/* 2 */}
      <h2 className={h} style={accent}>2. How opportunities are found</h2>
      <p className={p} style={s}>
        Three steps happen every scan:
      </p>
      <ol className="list-decimal pl-6 mb-4">
        <li className={li} style={s}>
          <strong style={accent}>Remove the sportsbook margin.</strong> FanDuel builds a
          profit margin (vig) into their odds. The tool strips it out to get a de-vigged
          model probability &mdash; an estimate, not a known true probability. For soccer, the draw probability is included in this calculation.
        </li>
        <li className={li} style={s}>
          <strong style={accent}>Compare to the prediction market price.</strong> If the model
          probability is higher than the prediction market price, there&apos;s positive expected
          value. A cost buffer (1&cent; on Kalshi, none on Polymarket) is added to the
          market price before computing edge.
        </li>
        <li className={li} style={s}>
          <strong style={accent}>Run quality checks.</strong> Every opportunity passes through
          23 rules that verify the match is correct, the data is fresh, the teams are aligned
          to the right sides, and the edge is real &mdash; not a data error.
        </li>
      </ol>
      <div className="rounded border px-4 py-3 mb-4" style={{ borderColor: "rgba(19,78,74,0.3)", background: "rgba(13,20,22,0.6)" }}>
        <p className="font-mono text-[11px]" style={accent}>
          edge = true_probability &minus; (market_price + 1&cent;)
        </p>
      </div>
      <p className={p} style={s}>
        Example: FanDuel implies a team wins 60% of the time. Kalshi sells YES
        at 52&cent;. Edge = 60% &minus; 53% = <strong style={accent}>+7%</strong>.
      </p>

      {/* 3 */}
      <h2 className={h} style={accent}>3. What BUY vs WATCH means</h2>
      <ul className="pl-6 mb-4">
        <li className={li} style={s}>
          <strong style={{ color: "#2dd4bf" }}>BUY</strong> &mdash; All quality checks pass.
          The edge is above the minimum threshold (5%), the event match is confident, and
          there are no ambiguity flags. This is a signal worth investigating.
        </li>
        <li className={li} style={s}>
          <strong style={{ color: "#38bdf8" }}>WATCH</strong> &mdash; There is positive edge,
          but something is uncertain. The edge might be thin, the match confidence borderline,
          or there&apos;s a naming ambiguity between similar events. Worth monitoring, not
          acting on blindly.
        </li>
        <li className={li} style={s}>
          <strong style={dim}>SKIP</strong> &mdash; A hard rule failed. No edge, wrong side
          alignment, stale data, or a clear data error. These are filtered out and not shown.
        </li>
      </ul>
      <p className={p} style={s}>
        BUY does not mean &ldquo;guaranteed profit.&rdquo; It means the data checks passed
        and the math says this is a positive expected value bet. Always verify the prices
        on the actual platform before acting.
      </p>

      {/* 4 */}
      <h2 className={h} style={accent}>4. How CLV works</h2>
      <p className={p} style={s}>
        CLV (Closing Line Value) is the main metric for measuring whether you&apos;re
        actually beating the market. It answers: <em>&ldquo;Did FanDuel&apos;s closing
        line move toward my position?&rdquo;</em>
      </p>
      <p className={p} style={s}>
        When you take a position, the FanDuel devigged probability at that moment is
        recorded. When the game starts, the tool fetches the latest available FanDuel
        odds snapshot from just before kickoff and devigs it the same way. CLV is the
        difference between the two:
      </p>
      <div className="rounded border px-4 py-3 mb-4" style={{ borderColor: "rgba(19,78,74,0.3)", background: "rgba(13,20,22,0.6)" }}>
        <p className="font-mono text-[11px]" style={accent}>
          Entry EV = FD_entry_probability &minus; prediction_market_price
        </p>
        <p className="font-mono text-[11px] mt-1" style={accent}>
          CLV = FD_closing_probability &minus; prediction_market_price
        </p>
      </div>
      <p className={p} style={s}>
        <strong style={accent}>Entry EV</strong> is the edge the model saw when you
        took the bet. It compares what you paid on the prediction market to what
        FanDuel implied at that moment.
      </p>
      <p className={p} style={s}>
        <strong style={accent}>CLV</strong> is the edge you actually had against the
        final closing line. It compares what you paid to what FanDuel implied at
        kickoff &mdash; generally the best-informed pre-game number available to the tool.
      </p>
      <p className={p} style={s}>
        Example: You buy Dallas at 59&cent; on Kalshi. FanDuel has Dallas at 63%
        (Entry EV = <strong style={accent}>+4%</strong>). By kickoff, FanDuel has
        moved to 60.7% (CLV = <strong style={accent}>+1.7%</strong>). You still had
        real edge against the closing line, but less than the model predicted.
      </p>
      <p className={p} style={s}>
        <strong>Why FanDuel is the benchmark:</strong> FanDuel&apos;s closing line
        reflects the final consensus of sharp bettors and professional bookmakers.
        It is a widely used proxy for a well-informed pre-game probability. By measuring your
        entry against this line, you&apos;re measuring whether you were ahead of
        the sharpest money in the market.
      </p>
      <p className={p} style={s}>
        <strong>Why CLV matters more than win/loss:</strong> Any single bet can win or
        lose regardless of edge. A 60% favorite still loses 40% of the time. But if the
        FanDuel closing line consistently moves toward your positions, you have real
        edge &mdash; and that edge turns into profit over many bets.
      </p>
      <p className={p} style={s}>
        <strong>Comparing the two:</strong> If Entry EV is consistently higher than CLV,
        the model is overestimating edge (the line moves against you before kickoff). If
        CLV is consistently positive, you&apos;re genuinely beating the closing line
        regardless of what the model predicted. Positive average CLV over 30+ bets is
        the strongest evidence that your process is working.
      </p>

      {/* 5 */}
      <h2 className={h} style={accent}>5. How to use this tool</h2>
      <ol className="list-decimal pl-6 mb-4">
        <li className={li} style={s}>
          <strong>Scan.</strong> Let the scanner run. BUY signals are the starting point,
          not the final answer.
        </li>
        <li className={li} style={s}>
          <strong>Verify.</strong> Open the prediction market link and check that the price
          matches what the scanner shows. Confirm the correct team is on the correct side.
          If something looks off, it probably is.
        </li>
        <li className={li} style={s}>
          <strong>Size conservatively.</strong> The Kelly recommendation shown is already at
          1/5 Kelly (conservative). Treat it as a ceiling, not a target. If you&apos;re
          unsure about the match quality, bet less.
        </li>
        <li className={li} style={s}>
          <strong>Track your positions.</strong> Mark opportunities as taken. When the event
          starts, the tool records the closing line and computes your CLV automatically.
        </li>
        <li className={li} style={s}>
          <strong>Think in samples, not single bets.</strong> A +5% edge loses about 45% of
          the time. The edge shows up over 50&ndash;100+ bets, not one game. If your average
          CLV is positive over many bets, you&apos;re doing it right.
        </li>
      </ol>

      {/* 6 */}
      <h2 className={h} style={warn}>6. Limitations</h2>
      <ul className="pl-6 mb-4">
        <li className={li} style={s}>
          <strong style={warn}>Stale prices.</strong> FanDuel odds refresh every 15 minutes.
          Prediction market prices are from the last scan. Near game time, lines can move
          fast. An edge that appears on screen may be gone by the time you trade. Always
          check the live price.
        </li>
        <li className={li} style={s}>
          <strong style={warn}>Matching errors.</strong> The tool matches events by team names
          and dates across different platforms. Unusual team names, abbreviations, or scheduling
          conflicts (back-to-back series) can cause mismatches. The rule engine catches most
          of these, but subtle errors can slip through.
        </li>
        <li className={li} style={s}>
          <strong style={warn}>Uneven sport coverage.</strong> FanDuel coverage varies by sport.
          Some sports have tight, liquid lines (NBA, NHL). Others have wider margins that can
          create false-positive edges.
        </li>
        <li className={li} style={s}>
          <strong style={warn}>No trade execution.</strong> The tool finds opportunities &mdash;
          it doesn&apos;t execute trades. You must place bets manually. Prices can change
          between when you see the signal and when you trade.
        </li>
        <li className={li} style={s}>
          <strong style={warn}>CLV precision.</strong> The closing line uses the latest
          available FanDuel historical snapshot before game start (5-minute intervals).
          If FanDuel odds are unavailable for an event, CLV cannot be computed.
        </li>
      </ul>

      <div className="rounded border px-4 py-3 mt-8 mb-4" style={{ borderColor: "rgba(75,85,99,0.2)", background: "rgba(75,85,99,0.04)" }}>
        <p className="font-mono text-[10px]" style={dim}>
          This tool is for informational purposes. It does not execute trades. Always do your
          own research before placing bets. Past edge calculations do not guarantee future results.
        </p>
      </div>
    </main>
  );
}
