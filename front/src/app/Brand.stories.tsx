import { Brand, BrandMark } from "./Brand";

export default { title: "Identity / Brand" };

export const Wordmark = () => <Brand />;

export const Large = () => <Brand markSize={26} textClass="text-[24px]" />;

export const MarkOnly = () => <BrandMark size={48} />;
