import json
import os
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks


class SkyEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.json_path = "sky_events.json"
        self.events_data = self.load_data()

        # لمنع تكرار الرسالة في نفس الدقيقة
        self.sent_alerts = set()

        # لمنع إرسال عداد بداية اليوم أكثر من مرة
        self.sent_daily_countdowns = set()

        self.events_checker.start()

    def load_data(self):
        if os.path.exists(self.json_path):
            with open(self.json_path, "r", encoding="utf-8") as f:
                return json.load(f)

        return {}

    def cog_unload(self):
        self.events_checker.cancel()

    # ---------------------------------------------------------
    # حساب وقت الريسيت اليومي الأخير
    # ---------------------------------------------------------
    def get_last_daily_reset_utc(self):
        now_utc = datetime.now(timezone.utc)

        reset_hour = int(
            self.events_data.get("config", {})
            .get("daily_reset_utc", "07:00")
            .split(":")[0]
        )

        reset_time_today = now_utc.replace(
            hour=reset_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        if now_utc < reset_time_today:
            return reset_time_today - timedelta(days=1)

        return reset_time_today

    # ---------------------------------------------------------
    # المحرك الرئيسي
    # ---------------------------------------------------------
    @tasks.loop(minutes=1)
    async def events_checker(self):
        await self.bot.wait_until_ready()

        channel_id = (
            self.events_data.get("config", {}).get("channel_id")
            or int(os.getenv("SKY_EVENTS_CHANNEL_ID", "0"))
        )

        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)

        if not channel:
            return

        now_utc = datetime.now(timezone.utc)
        now_minute = now_utc.replace(second=0, microsecond=0)

        last_reset = self.get_last_daily_reset_utc()

        minutes_since_reset = int(
            (now_utc - last_reset).total_seconds() // 60
        )

        # =====================================================
        # 1. الأحداث الدورية - كل ساعتين
        # =====================================================
        for event in self.events_data.get("interval_events", []):
            repeat_hours = event.get("repeat_interval_hours", 2)
            cycle_minutes = repeat_hours * 60

            cycle_minute = minutes_since_reset % cycle_minutes

            alert_offset = event["alert_offset_minutes"]
            start_offset = event["start_offset_minutes"]
            end_offset = event["end_offset_minutes"]

            current_minute_key = now_minute.strftime("%Y%m%d%H%M")

            # -------------------------------------------------
            # تنبيه الحدث
            # -------------------------------------------------
            if cycle_minute == alert_offset:
                alert_key = f"{event['id']}_alert_{current_minute_key}"

                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "تنبيه الحدث 🔔",
                        "سيبدأ الحدث بعد قليل!",
                        discord.Color.gold()
                    )

                    self.sent_alerts.add(alert_key)

            # -------------------------------------------------
            # بدء الحدث
            # -------------------------------------------------
            elif cycle_minute == start_offset:
                alert_key = f"{event['id']}_start_{current_minute_key}"

                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "بدء الحدث 🟢",
                        "بدأ الحدث الآن!",
                        discord.Color.green()
                    )

                    self.sent_alerts.add(alert_key)

            # -------------------------------------------------
            # انتهاء الحدث
            # -------------------------------------------------
            elif cycle_minute == (end_offset % cycle_minutes):
                alert_key = f"{event['id']}_end_{current_minute_key}"

                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "انتهاء الحدث 🔴",
                        "انتهى الحدث الآن.",
                        discord.Color.red()
                    )

                    self.sent_alerts.add(alert_key)

        # =====================================================
        # 2. الأحداث المجدولة
        # =====================================================
        scheduled_events = self.events_data.get(
            "scheduled_events", []
        )

        await self.check_scheduled_events(
            channel,
            scheduled_events,
            now_utc
        )

        # =====================================================
        # 3. الأحداث الحالية
        # =====================================================
        current_events = self.events_data.get(
            "events_current", []
        )

        await self.check_scheduled_events(
            channel,
            current_events,
            now_utc
        )

        # =====================================================
        # 4. عداد الأيام عند بداية اليوم الجديد
        # =====================================================
        await self.send_daily_countdowns(
            channel,
            scheduled_events,
            current_events,
            now_utc,
            last_reset
        )

        # -----------------------------------------------------
        # تنظيف ذاكرة التنبيهات
        # -----------------------------------------------------
        if len(self.sent_alerts) > 500:
            self.sent_alerts.clear()

        if len(self.sent_daily_countdowns) > 100:
            self.sent_daily_countdowns.clear()

    # ---------------------------------------------------------
    # فحص الأحداث المجدولة
    # ---------------------------------------------------------
    async def check_scheduled_events(
        self,
        channel,
        events,
        now_utc
    ):
        now_minute = now_utc.replace(
            second=0,
            microsecond=0
        )

        for event in events:

            # إذا كان الحدث متوقفًا، تجاهله بالكامل
            if not event.get("enabled", True):
                continue

            start_time = self.parse_iso_time(
                event.get("start_time_iso")
            )

            end_time = self.parse_iso_time(
                event.get("end_time_iso")
            )

            if not start_time or not end_time:
                continue

            current_minute_key = now_minute.strftime(
                "%Y%m%d%H%M"
            )

            # -------------------------------------------------
            # بدء الحدث
            # -------------------------------------------------
            if now_minute == start_time:
                alert_key = (
                    f"{event['id']}_start_{current_minute_key}"
                )

                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "بدء الحدث 🟢",
                        "بدأ الحدث الآن!",
                        discord.Color.green()
                    )

                    self.sent_alerts.add(alert_key)

            # -------------------------------------------------
            # انتهاء الحدث
            # -------------------------------------------------
            elif now_minute == end_time:
                alert_key = (
                    f"{event['id']}_end_{current_minute_key}"
                )

                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "انتهاء الحدث 🔴",
                        "انتهى الحدث الآن.",
                        discord.Color.red()
                    )

                    self.sent_alerts.add(alert_key)

    # ---------------------------------------------------------
    # إرسال عداد الأيام عند بداية اليوم الجديد فقط
    # ---------------------------------------------------------
    async def send_daily_countdowns(
        self,
        channel,
        scheduled_events,
        current_events,
        now_utc,
        last_reset
    ):
        # التأكد من أننا في أول 5 دقائق بعد الريسيت اليومي
        minutes_since_reset = int((now_utc - last_reset).total_seconds() // 60)
        if minutes_since_reset > 5:
            return

        reset_key = last_reset.strftime("%Y%m%d")

        if reset_key in self.sent_daily_countdowns:
            return

        events = scheduled_events + current_events
        countdowns_sent = False

        for event in events:
            if not event.get("enabled", True):
                continue

            start_time = self.parse_iso_time(event.get("start_time_iso"))
            end_time = self.parse_iso_time(event.get("end_time_iso"))

            if not start_time or not end_time:
                continue

            # الحدث يجب أن يكون بدأ بالفعل ولم ينتهِ بعد
            if now_utc < start_time or now_utc >= end_time:
                continue

            # حساب الأيام المتبقية
            remaining_seconds = (end_time - now_utc).total_seconds()
            remaining_days = int(remaining_seconds // 86400)

            if remaining_days <= 0:
                days_text = "أقل من يوم واحد"
            elif remaining_days == 1:
                days_text = "يوم واحد"
            elif remaining_days == 2:
                days_text = "يومان"
            elif 3 <= remaining_days <= 10:
                days_text = f"{remaining_days} أيام"
            else:
                days_text = f"{remaining_days} يومًا"

            embed = discord.Embed(
                title=f"{event.get('icon', '✨')} {event.get('name_ar', 'حدث')} — [الوقت المتبقي ⏳]",
                description=f"**المتبقي حتى انتهاء الحدث: {days_text}**",
                color=discord.Color.blue(),
                timestamp=now_utc
            )
            embed.add_field(name="🌍 العالم (Realm)", value=event.get("realm", "غير محدد"), inline=True)
            embed.add_field(name="📍 المنطقة (Area)", value=event.get("area", "غير محدد"), inline=True)
            embed.set_footer(text="Sky: Children of the Light • التوقيت التلقائي")

            await channel.send(embed=embed)
            countdowns_sent = True

        if countdowns_sent:
            self.sent_daily_countdowns.add(reset_key)
            
    # ---------------------------------------------------------
    # تحويل ISO إلى datetime
    # ---------------------------------------------------------
    def parse_iso_time(self, value):
        if not value:
            return None

        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).replace(
                second=0,
                microsecond=0
            )

        except (ValueError, TypeError):
            return None

    # ---------------------------------------------------------
    # بناء وإرسال Embed
    # ---------------------------------------------------------
    async def send_event_embed(
        self,
        channel,
        event,
        status_title,
        description,
        color
    ):
        embed = discord.Embed(
            title=(
                f"{event.get('icon', '✨')} "
                f"{event.get('name_ar', 'حدث')} "
                f"— [{status_title}]"
            ),
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="🌍 العالم (Realm)",
            value=event.get("realm", "غير محدد"),
            inline=True
        )

        embed.add_field(
            name="📍 المنطقة (Area)",
            value=event.get("area", "غير محدد"),
            inline=True
        )

        embed.set_footer(
            text="Sky: Children of the Light • التوقيت التلقائي"
        )

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(SkyEvents(bot))