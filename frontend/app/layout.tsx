import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

// Inter for UI text. Google Sans / Google Sans Flex are not yet exposed via
// next/font/google as of Next 15.x; using Inter (variable font, OFL) as the
// safe long-term fallback. Switch to "Google_Sans_Flex" when next/font/google
// adds it. JetBrains Mono provides tabular numerals for the data columns.
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Prediction Market Scanner",
  description: "Cross-platform EV scanner for prediction market opportunities",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        className={`${inter.variable} ${jetbrainsMono.variable} min-h-screen antialiased`}
        style={{ background: "#07090a", color: "#e2e8f0", fontFamily: "var(--font-sans)" }}
      >
        <header
          className="h-12 flex items-center px-6 border-b"
          style={{ borderColor: "rgba(19,78,74,0.3)", background: "#080b0c" }}
        >
          <div className="flex items-center gap-2.5 mr-10">
            <span style={{ color: "#2dd4bf" }} className="text-base leading-none">&#9670;</span>
            <span
              className="font-mono font-bold text-[13px] tracking-[0.2em] uppercase"
              style={{ color: "#e2e8f0" }}
            >
              Market Scanner
            </span>
            <span
              className="font-mono text-[8px] tracking-wider px-1.5 py-0.5 rounded uppercase ml-1"
              style={{ color: "#374151", background: "rgba(75,85,99,0.08)" }}
            >
              Beta
            </span>
          </div>

          <nav className="flex items-center gap-6">
            <Link
              href="/"
              className="nav-link text-[11px] font-mono tracking-widest uppercase transition-colors"
              style={{ color: "#4b7280" }}
            >
              Scanner
            </Link>
            <Link
              href="/model"
              className="nav-link text-[11px] font-mono tracking-widest uppercase transition-colors"
              style={{ color: "#4b7280" }}
            >
              Model
            </Link>
          </nav>
        </header>

        {children}
      </body>
    </html>
  );
}
