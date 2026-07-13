export type BusyAction = "turn" | "review" | "approve" | null;
export type Notice = { kind: "success" | "error"; text: string };
export type TitleChoice =
  | { kind: "recommended"; title: string }
  | { kind: "custom"; title: string }
  | null;
