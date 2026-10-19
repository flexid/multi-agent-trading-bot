"use client";
import { useState } from "react";

export default function ShareLink() {
  const [label, setLabel] = useState("share");
  async function copy(e: React.MouseEvent) {
    e.preventDefault();
    const url = "https://dex.dorkbot.dev/";
    try {
      if (navigator.share) { await navigator.share({ url }); return; }
      await navigator.clipboard.writeText(url);
      setLabel("link copied");
      setTimeout(() => setLabel("share"), 2000);
    } catch {
      window.prompt("Copy this link:", url);
    }
  }
  return <a href="https://dex.dorkbot.dev/" onClick={copy}>{label}</a>;
}
