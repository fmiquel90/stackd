import { useEffect, useState } from "react";
import { mergeChunks } from "@/lib/logModel";
import { LogViewer } from "./LogViewer";
import { LOG_ANSI, LOG_FAILED_HOOK, bigLog } from "./stories.fixtures";

export default { title: "Identity / LogViewer" };

export const FailedHookAndFirstError = () => (
  <LogViewer
    chunks={LOG_FAILED_HOOK}
    live={false}
    failedHooks={["policy: require owner tag"]}
    className="h-[420px]"
  />
);

export const TargetedLine = () => (
  <LogViewer chunks={LOG_FAILED_HOOK} live={false} initialLine="planning.4" className="h-[320px]" />
);

export const AnsiPalette = () => <LogViewer chunks={LOG_ANSI} live={false} className="h-[420px]" />;

export const LiveFollowing = () => <LogViewer chunks={bigLog(400)} live className="h-[420px]" />;

export const TwentyThousandLines = () => (
  <LogViewer chunks={bigLog(20000)} live={false} className="h-[520px]" />
);

// A live apply on top of a 20 000-line plan: 200 lines every 250 ms, each chunk processed alone.
export const StreamingApply = () => {
  const [chunks, setChunks] = useState(() => bigLog(20000));
  useEffect(() => {
    let seq = 0;
    const id = setInterval(() => {
      const at = seq++;
      const lines = Array.from({ length: 200 }, (_, i) => ({
        t: "20:01:00",
        msg: `aws_subnet.private[${at * 200 + i}]: Creating...`,
      }));
      setChunks((c) => mergeChunks(c, [{ phase: "applying", seq: at, section: null, lines }]));
    }, 250);
    return () => clearInterval(id);
  }, []);
  return <LogViewer chunks={chunks} live className="h-[520px]" />;
};

export const Empty = () => (
  <div className="flex flex-col gap-3">
    <LogViewer chunks={[]} live className="h-[120px]" />
    <LogViewer chunks={[]} live={false} className="h-[120px]" />
  </div>
);
