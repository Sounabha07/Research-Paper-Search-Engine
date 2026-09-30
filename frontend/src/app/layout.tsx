import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "arXiv Research Engine",
  description: "Answers from arXiv CS and math papers, every sentence cited",
};

// Follow the OS color scheme before first paint to avoid a flash.
const themeScript = `(() => {
  const m = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => document.documentElement.classList.toggle("dark", m.matches);
  apply();
  m.addEventListener("change", apply);
})();`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <nav className="border-b border-border">
          <div className="mx-auto flex max-w-5xl items-center gap-5 px-4 py-2.5 text-sm">
            <Link href="/" className="font-semibold tracking-tight">
              arXiv Research Engine
            </Link>
            <Link href="/search" className="text-muted-foreground hover:text-foreground">
              Search
            </Link>
            <Link href="/admin" className="text-muted-foreground hover:text-foreground">
              Admin
            </Link>
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
