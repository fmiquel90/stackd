import { useState } from "react";
import { Checkbox } from "./ui";

export default { title: "UI / Checkbox" };

function Demo() {
  const [a, setA] = useState(true);
  const [b, setB] = useState(false);
  return (
    <div className="flex w-[280px] flex-col gap-3 bg-surface p-4">
      <Checkbox checked={a} onChange={setA} label="sensitive" />
      <Checkbox checked={b} onChange={setB} label="managed state" />
      <Checkbox checked onChange={() => {}} label="checked + disabled" disabled />
      <Checkbox checked={false} onChange={() => {}} label="unchecked + disabled" disabled />
    </div>
  );
}

export const Default = () => <Demo />;
