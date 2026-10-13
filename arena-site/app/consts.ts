/* Plain constants, importable from server and client code alike. */
export const TRADERS = ["kwikton", "kwekton", "kwakton"] as const;
export type TraderId = (typeof TRADERS)[number];
