import { TypeToConfirm } from "./TypeToConfirm";

export default { title: "Identity / TypeToConfirm" };

const noop = () => {};

export const FourEyesApply = () => (
  <div className="w-fit border border-control bg-raised p-3">
    <TypeToConfirm
      name="prod"
      verb="apply"
      because={["tier prod"]}
      counts={{ add: 4, change: 0, destroy: 1 }}
      destroyed={["terraform_data.nat_gateway"]}
      replaced={[]}
      cascade={[
        {
          label: "demo-app/prod",
          likelihood: "plans_if_outputs_change",
          outputs: ["network_name"],
          mock: true,
        },
      ]}
      onConfirm={noop}
      onCancel={noop}
    />
  </div>
);

export const DestroyLongList = () => (
  <div className="w-fit border border-control bg-raised p-3">
    <TypeToConfirm
      name="staging"
      verb="destroy"
      because={["destroy run"]}
      counts={{ add: 0, change: 0, destroy: 40 }}
      destroyed={Array.from({ length: 40 }, (_, i) => `aws_subnet.private[${i}]`)}
      replaced={["aws_instance.web"]}
      cascade={[]}
      onConfirm={noop}
      onCancel={noop}
    />
  </div>
);

export const Submitting = () => (
  <div className="w-fit border border-control bg-raised p-3">
    <TypeToConfirm
      name="prod"
      verb="apply"
      because={["protected environment"]}
      counts={{ add: 1, change: 0, destroy: 0 }}
      destroyed={[]}
      replaced={[]}
      cascade={[]}
      pending
      onConfirm={noop}
      onCancel={noop}
    />
  </div>
);
