// Local mock data for Brindle, a fictional bank. Nothing here is real.

/** Marks a literal as a Pineo element id so `pnpm kb:check` can see it. */
const pineo = (id: string) => id;

export const USER = {
  first: "Noa",
  name: "Noa Lindqvist",
  email: "noa.lindqvist@example.com",
  phone: "+1 (415) 555-0142",
  address: "14 Harbour Lane, Apt 3, San Francisco, CA",
  tag: "@noa.l",
};

export interface Account {
  id: string;
  name: string;
  detail: string;
  balance: number;
  tone: string;
}

export const ACCOUNTS: Account[] = [
  { id: "everyday", name: "Everyday", detail: "Main account ·· 4821", balance: 4280.55, tone: "plum" },
  { id: "savings", name: "Rainy day", detail: "Savings · 3.1% APY", balance: 12940.0, tone: "teal" },
  { id: "travel", name: "Lisbon trip", detail: "Pot · goal $1,500", balance: 615.2, tone: "tangerine" },
];

export const usd = (n: number, sign = false) => {
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const prefix = n < 0 ? "−" : sign ? "+" : "";
  return `${prefix}$${abs}`;
};

export type TxKind = "card" | "transfer" | "income" | "bill";

export interface Tx {
  id: string;
  pineoId?: string;
  day: number; // day of month
  month: "Nov" | "Oct";
  time: string;
  who: string;
  what: string;
  kind: TxKind;
  amount: number;
  /** Overrides the printed date. */
  dateLabel?: string;
}

type Row = [number, string, string, string, TxKind, number, string?, string?];

const NOV: Row[] = [
  [14, "08:12", "Kettle & Crumb", "Coffee · Card ·· 4821", "card", -4.6, pineo("activity-tx-latest")],
  [14, "07:55", "Metro day pass", "Transport", "card", -6.4],
  [13, "19:40", "Tessaly Market", "Groceries", "card", -62.18],
  [13, "13:05", "Ilse Brandt", "Transfer · dinner split", "transfer", 28.0],
  [13, "09:12", "Volta Energy", "Bill · electricity", "bill", -84.3, pineo("activity-tx-wrong-date"), "31 Nov"],
  [12, "21:15", "Lumen Cinema", "Entertainment", "card", -19.0],
  [12, "12:31", "Noodle Arcade", "Eating out", "card", -14.5],
  [11, "16:02", "Fernhill Pharmacy", "Health", "card", -11.95],
  [11, "08:00", "Rainy day", "Transfer to savings", "transfer", -200.0],
  [10, "18:47", "Tessaly Market", "Groceries", "card", -38.42],
  [9, "11:20", "Orbit Mobile", "Bill · phone plan", "bill", -22.0],
  [8, "20:05", "Saffron Table", "Eating out", "card", -47.8],
  [8, "10:10", "Paperbark Books", "Shopping", "card", -23.9],
  [7, "09:00", "Harbour Lane Lettings", "Rent · November", "bill", -1150.0, pineo("activity-tx-rent")],
  [6, "17:33", "Cyclewise", "Bike repair", "card", -55.0],
  [5, "08:26", "Kettle & Crumb", "Coffee", "card", -4.6],
  [4, "14:12", "Mateo Ruiz", "Transfer · concert tickets", "transfer", -72.0],
  [3, "19:58", "Streamline+", "Subscription", "bill", -11.99],
  [2, "12:40", "Greenhouse Deli", "Eating out", "card", -9.75],
  [1, "07:30", "Northwind Studio", "Salary · October", "income", 3240.0, pineo("activity-tx-salary")],
  [1, "07:31", "Brindle", "Interest · Rainy day", "income", 33.4],
];

const OCT: Row[] = [
  [31, "22:10", "Night Owl Cabs", "Transport", "card", -17.3],
  [30, "18:20", "Tessaly Market", "Groceries", "card", -71.06],
  [29, "13:15", "Noodle Arcade", "Eating out", "card", -13.0],
  [28, "09:45", "Fjord Outfitters", "Shopping", "card", -129.0],
  [27, "16:30", "Fjord Outfitters", "Refund · jacket", "income", 129.0, pineo("activity-tx-refund")],
  [26, "11:05", "Kettle & Crumb", "Coffee", "card", -4.6],
  [25, "20:40", "Saffron Table", "Eating out", "card", -52.4],
  [24, "08:00", "Rainy day", "Transfer to savings", "transfer", -200.0],
  [23, "17:10", "Fernhill Pharmacy", "Health", "card", -8.2],
  [22, "12:22", "Greenhouse Deli", "Eating out", "card", -10.5],
  [21, "19:00", "Aqua Leisure Centre", "Membership", "bill", -35.0],
  [20, "10:15", "Ilse Brandt", "Transfer · birthday gift", "transfer", -40.0],
  [19, "15:48", "Tessaly Market", "Groceries", "card", -44.91],
  [18, "09:30", "Orbit Mobile", "Bill · phone plan", "bill", -22.0],
  [17, "21:25", "Lumen Cinema", "Entertainment", "card", -19.0],
  [16, "08:14", "Metro monthly", "Transport", "card", -58.0],
  [15, "13:50", "Paperbark Books", "Shopping", "card", -15.5],
  [14, "18:05", "Volta Energy", "Bill · electricity", "bill", -79.1],
  [13, "12:00", "Mateo Ruiz", "Transfer · lunch", "transfer", 16.5],
  [12, "09:20", "Kettle & Crumb", "Coffee", "card", -4.6],
  [10, "17:45", "Cyclewise", "Helmet", "card", -39.0],
  [8, "20:30", "Streamline+", "Subscription", "bill", -11.99],
  [7, "09:00", "Harbour Lane Lettings", "Rent · October", "bill", -1150.0],
  [3, "14:40", "Tessaly Market", "Groceries", "card", -57.33],
  [1, "07:30", "Northwind Studio", "Salary · September", "income", 3240.0],
];

const build = (rows: Row[], month: "Nov" | "Oct"): Tx[] =>
  rows.map(([day, time, who, what, kind, amount, pineoId, dateLabel], i) => ({
    id: `${month}-${i}`,
    day,
    month,
    time,
    who,
    what,
    kind,
    amount,
    pineoId,
    dateLabel,
  }));

export const TRANSACTIONS: Tx[] = [...build(NOV, "Nov"), ...build(OCT, "Oct")];

/** "Today" in the mock world. */
export const TODAY = { day: 14, month: "Nov" as const };

export const RECIPIENTS = [
  { name: "Ilse Brandt", iban: "021000021 · 5567120388", color: "#7a5cff" },
  { name: "Mateo Ruiz", iban: "021000021 · 1198441005", color: "#ff7a3d" },
  { name: "Harbour Lane Lettings", iban: "121000358 · 7730221640", color: "#0f9d8a" },
  { name: "Suvi Aalto", iban: "021000021 · 6402991721", color: "#e0457b" },
];

export const CARDS = [
  { id: "everyday", name: "Everyday debit", last4: "4821", full: "5290 1177 3046 4821", exp: "09/29", pin: "4071", tone: "plum" },
  { id: "travel", name: "Travel card", last4: "7306", full: "5290 1183 9920 7306", exp: "02/28", pin: "1958", tone: "tangerine" },
  { id: "virtual", name: "Online-only virtual", last4: "0159", full: "5290 1190 4471 0159", exp: "12/27", pin: "6620", tone: "ink" },
];
