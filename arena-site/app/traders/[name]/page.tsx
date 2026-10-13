import { TRADERS } from "../../consts";
import TraderPage from "./trader";

// Static export: one page per trader, the name read from the path.
export const dynamicParams = false;
export function generateStaticParams() {
  return TRADERS.map((name) => ({ name }));
}

export default async function Page({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return <TraderPage id={name as (typeof TRADERS)[number]} />;
}
