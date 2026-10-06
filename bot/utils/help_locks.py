"""🔒 marks in /help for commands the server's plan doesn't include.

Which commands are gated comes from their requires_feature checks, so a
newly gated command is marked without touching this module. Nothing is
marked while tiers aren't enforced: every command still works then, and
pointing at plans nobody needs yet would only confuse.
"""

from __future__ import annotations

from bot.utils.tiers import TierPolicy, command_features, lowest_tier_with


def locked_commands(commands, policy: TierPolicy) -> dict[str, TierPolicy]:
    """{qualified name: cheapest tier unlocking it} for commands ``policy`` lacks."""
    return {
        name: lowest_tier_with(feature)
        for name, feature in command_features(commands).items()
        if not policy.allows(feature)
    }


def _names_command(line: str, name: str) -> bool:
    rest = line.lstrip()
    prefix = f"/{name}"
    return rest.startswith(prefix) and rest[len(prefix):len(prefix) + 1] in ("", " ", "\n")


def mark_locked(text: str, locked: dict[str, TierPolicy]) -> str:
    """Append "🔒 <tier>" to each help line that lists a locked command."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        tier = next((t for name, t in locked.items() if _names_command(line, name)), None)
        if tier is not None:
            body = line.rstrip("\n")
            lines[i] = f"{body}  🔒 {tier.name}" + line[len(body):]
    return "".join(lines)


async def help_with_locks(interaction, text: str, commands) -> str:
    """``text`` with 🔒 marks and a /premium footer, if anything is locked."""
    tiers = getattr(interaction.client, "tiers", None)
    if tiers is None or not tiers.enforced or interaction.guild_id is None:
        return text
    status = await tiers.status(str(interaction.guild_id))
    locked = locked_commands(commands, status.policy)
    if not locked:
        return text
    footer = (
        f"🔒 Not included in this server's **{status.policy.name}** plan; "
        "the plan named unlocks it. Run `/premium` to compare plans."
    )
    return f"{mark_locked(text, locked).rstrip()}\n\n{footer}\n"
