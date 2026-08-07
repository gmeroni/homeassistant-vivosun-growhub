"""Humidifier platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
)
from homeassistant.components.humidifier.const import HumidifierEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    HMDF_DEFAULT_AUTO_TARGET,
    HMDF_DEFAULT_MANUAL_LEVEL,
    HMDF_MODE_MANUAL,
    HMDF_MODE_OFF,
    TEMP_SCALE_FACTOR,
)
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, sensor_slice, shadow_slice
from .shadow import (
    build_dhmdf_on_payload,
    build_dhmdf_target_payload,
    build_hmdf_auto_payload,
    build_hmdf_manual_payload,
    build_hmdf_off_payload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_HMDF_MODES = ("manual", "auto")
_TURN_ON_FEATURE = getattr(HumidifierEntityFeature, "TURN_ON", HumidifierEntityFeature(0))
_TURN_OFF_FEATURE = getattr(HumidifierEntityFeature, "TURN_OFF", HumidifierEntityFeature(0))
_EXPLICIT_TURN_FEATURES = _TURN_ON_FEATURE | _TURN_OFF_FEATURE


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun humidifier and dehumidifier entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    entities: list[Entity] = []
    humidifiers = [d for d in coordinator.devices if d.device_type == "humidifier"]
    if humidifiers:
        entities.extend([VivosunHumidifierEntity(coordinator, d.device_id) for d in humidifiers])
    dehumidifiers = [d for d in coordinator.devices if d.device_type == "dehumidifier"]
    if dehumidifiers:
        entities.extend([VivosunDehumidifierEntity(coordinator, d.device_id) for d in dehumidifiers])
    if entities:
        async_add_entities(entities)

class VivosunHumidifierEntity(CoordinatorEntity[VivosunCoordinator], HumidifierEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroStream humidifier."""

    _attr_has_entity_name = True
    _attr_name = "Humidifier"
    _attr_device_class = HumidifierDeviceClass.HUMIDIFIER
    _attr_supported_features = _EXPLICIT_TURN_FEATURES | HumidifierEntityFeature.MODES
    _attr_min_humidity = 0
    _attr_max_humidity = 100
    _enable_turn_on_off_backwards_compatibility = HumidifierEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the humidifier entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_humidifier"
        self._attr_available_modes = list(_HMDF_MODES)

    @property
    def is_on(self) -> bool | None:
        """Return whether the humidifier is on."""
        hmdf = self._hmdf_state()
        on = hmdf.get("on")
        if isinstance(on, bool):
            return on
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return the target humidity percentage."""
        hmdf = self._hmdf_state()
        target = hmdf.get("target_humidity")
        if isinstance(target, int):
            return target / TEMP_SCALE_FACTOR
        return None

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity from probe sensor."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pHumi")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def mode(self) -> str | None:
        """Return the current humidifier mode."""
        hmdf = self._hmdf_state()
        mode = hmdf.get("mode")
        if not isinstance(mode, int):
            return None
        if mode == HMDF_MODE_MANUAL:
            return "manual"
        if mode == HMDF_MODE_OFF:
            return None
        # Auto (2) and any other running mode (cycle/timer) surface as auto.
        return "auto"

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return additional humidifier state attributes."""
        hmdf = self._hmdf_state()
        attrs: dict[str, object] = {}
        level = hmdf.get("level")
        if isinstance(level, int):
            attrs["level"] = level
        water_warning = hmdf.get("water_warning")
        if isinstance(water_warning, bool):
            attrs["water_warning"] = water_warning
        return attrs

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn on the humidifier, re-asserting the currently selected mode.

        Auto (the humidistat) is the default; manual is only used when it is the
        active mode, since it runs at a fixed mist level rather than to a target.
        """
        _ = kwargs
        if self._hmdf_state().get("mode") == HMDF_MODE_MANUAL:
            payload = build_hmdf_manual_payload(HMDF_DEFAULT_MANUAL_LEVEL)
        else:
            payload = build_hmdf_auto_payload(self._auto_target_raw())
        await self.coordinator.async_publish_shadow_update(payload, device_id=self._device_id)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn off the humidifier (mode 0)."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_off_payload(), device_id=self._device_id
        )

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity and engage auto (mode 2 + hmdf.auto.tHumi).

        A humidity target is a humidistat request, so this also switches the device
        into auto mode — writing the target alone leaves the device idle.
        """
        target_raw = int(humidity * TEMP_SCALE_FACTOR)
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_auto_payload(target_raw), device_id=self._device_id
        )

    async def async_set_mode(self, mode: str) -> None:
        """Set humidifier mode (manual or auto)."""
        if mode == "manual":
            await self.coordinator.async_publish_shadow_update(
                build_hmdf_manual_payload(HMDF_DEFAULT_MANUAL_LEVEL), device_id=self._device_id
            )
        elif mode == "auto":
            await self.coordinator.async_publish_shadow_update(
                build_hmdf_auto_payload(self._auto_target_raw()), device_id=self._device_id
            )

    def _auto_target_raw(self) -> int:
        """Return the current raw (basis-point) auto target, or a sensible default.

        Reads the raw shadow value to avoid a lossy /100 -> *100 round-trip through
        the float ``target_humidity`` property.
        """
        raw = self._hmdf_state().get("target_humidity")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        return HMDF_DEFAULT_AUTO_TARGET * TEMP_SCALE_FACTOR

    def _hmdf_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "hmdf")


class VivosunDehumidifierEntity(CoordinatorEntity[VivosunCoordinator], HumidifierEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroDrain dehumidifier."""

    _attr_has_entity_name = True
    _attr_name = "Dehumidifier"
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_supported_features = _EXPLICIT_TURN_FEATURES
    _attr_min_humidity = 30
    _attr_max_humidity = 90
    _enable_turn_on_off_backwards_compatibility = HumidifierEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the dehumidifier entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_dehumidifier"

    @property
    def is_on(self) -> bool | None:
        """Return whether the dehumidifier is active (not paused)."""
        dhmdf = self._dhmdf_state()
        on = dhmdf.get("on")
        if isinstance(on, bool):
            return on
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return the target humidity percentage."""
        dhmdf = self._dhmdf_state()
        target = dhmdf.get("target_humidity")
        if isinstance(target, int):
            return target / TEMP_SCALE_FACTOR
        return None

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity from probe sensor when available."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pHumi")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    async def async_turn_on(self, **kwargs: object) -> None:
        """Unpause the dehumidifier."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_dhmdf_on_payload(True), device_id=self._device_id
        )

    async def async_turn_off(self, **kwargs: object) -> None:
        """Pause the dehumidifier."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_dhmdf_on_payload(False), device_id=self._device_id
        )

    async def async_set_humidity(self, humidity: int) -> None:
        """Set target humidity percentage."""
        target_raw = int(humidity * TEMP_SCALE_FACTOR)
        await self.coordinator.async_publish_shadow_update(
            build_dhmdf_target_payload(target_raw), device_id=self._device_id
        )

    def _dhmdf_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "dhmdf")
