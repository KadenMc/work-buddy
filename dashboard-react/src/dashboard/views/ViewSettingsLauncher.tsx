import { GearSix } from "@phosphor-icons/react/GearSix";
import {
  useInRouterContext,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { IconButton } from "../../ui";
import {
  createSettingsNavigationState,
  useSettingsPageRoute,
} from "../../settings";
import type { ViewDefinition } from "../contributions/contracts";

export function ViewSettingsLauncher({
  definition,
}: {
  readonly definition: ViewDefinition;
}) {
  const isInRouter = useInRouterContext();
  if (!isInRouter) return null;

  return <RoutedViewSettingsLauncher definition={definition} />;
}

function RoutedViewSettingsLauncher({
  definition,
}: {
  readonly definition: ViewDefinition;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const reference = definition.settings;
  const route = useSettingsPageRoute(reference?.pageId);

  if (reference === undefined || route === undefined) return null;

  return (
    <span
      className="wb-view-settings-launcher-tooltip"
      title={reference.label}
    >
      <IconButton
        label={reference.label}
        icon={<GearSix weight="duotone" />}
        variant="ghost"
        size="small"
        className="wb-view-settings-launcher"
        onClick={() => {
          const currentPath = `${location.pathname}${location.search}${location.hash}`;
          navigate(route, {
            state: createSettingsNavigationState(
              currentPath,
              `/${definition.route}`,
              `Back to ${definition.displayName}`,
            ),
          });
        }}
      />
    </span>
  );
}

export default ViewSettingsLauncher;
