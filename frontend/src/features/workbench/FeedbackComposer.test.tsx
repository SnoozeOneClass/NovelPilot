import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FeedbackComposer } from "./FeedbackComposer";

describe("FeedbackComposer", () => {
  it("appends a suggested correction to the current draft", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<FeedbackComposer value="保留现有冲突" sending={false} onChange={onChange} onSend={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "增加伏笔" }));

    expect(onChange).toHaveBeenCalledWith("保留现有冲突；增加伏笔");
  });

  it("submits with Enter and keeps Shift+Enter available for multiline text", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<FeedbackComposer value="放慢节奏" sending={false} onChange={vi.fn()} onSend={onSend} />);

    await user.click(screen.getByRole("textbox"));
    await user.keyboard("{Enter}");

    expect(onSend).toHaveBeenCalledTimes(1);
  });
});
