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

        # لمنع تكرار التنبيهات في نفس الدقيقة
        self.sent_alerts = set()

        # لمنع إرسال ملخص بداية اليوم أكثر من مرة
        self.sent_daily_resets = set()

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
            hour=reset_hour, minute=0, second=0, microsecond=0
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

        channel_id = self.events_data.get("config", {}).get(
            "channel_id"
        ) or int(os.getenv("SKY_EVENTS_CHANNEL_ID", "0"))

        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        now_utc = datetime.now(timezone.utc)
        now_minute = now_utc.replace(second=0, microsecond=0)

        last_reset = self.get_last_daily_reset_utc()
        minutes_since_reset = int((now_utc - last_reset).total_seconds() // 60)

        # =====================================================
        # 1. إرسال ملخص بداية اليوم الجديد (Daily Reset Announcement)
        # =====================================================
        if minutes_since_reset <= 5:
            reset_key = last_reset.strftime("%Y%m%d")
            if reset_key not in self.sent_daily_resets:
                await self.send_daily_reset_summary(channel, now_utc, last_reset)
                self.sent_daily_resets.add(reset_key)

        # =====================================================
        # 2. الأحداث الدورية - كل ساعتين
        # =====================================================
        for event in self.events_data.get("interval_events", []):
            repeat_hours = event.get("repeat_interval_hours", 2)
            cycle_minutes = repeat_hours * 60

            cycle_minute = minutes_since_reset % cycle_minutes

            alert_offset = event["alert_offset_minutes"]
            start_offset = event["start_offset_minutes"]
            end_offset = event["end_offset_minutes"]

            current_minute_key = now_minute.strftime("%Y%m%d%H%M")

            # تنبيه الحدث
            if cycle_minute == alert_offset:
                alert_key = f"{event['id']}_alert_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "تنبيه الحدث • Event Alert 🔔",
                        "سيبدأ الحدث بعد قليل! • Starting soon!",
                        discord.Color.gold(),
                    )
                    self.sent_alerts.add(alert_key)

            # بدء الحدث
            elif cycle_minute == start_offset:
                alert_key = f"{event['id']}_start_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "بدء الحدث • Event Started 🟢",
                        "بدأ الحدث الآن! • Started now!",
                        discord.Color.green(),
                    )
                    self.sent_alerts.add(alert_key)

            # انتهاء الحدث
            elif cycle_minute == (end_offset % cycle_minutes):
                alert_key = f"{event['id']}_end_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "انتهاء الحدث • Event Ended 🔴",
                        "انتهى الحدث الآن. • Ended now.",
                        discord.Color.red(),
                    )
                    self.sent_alerts.add(alert_key)

        # =====================================================
        # 3. الأحداث المجدولة واللحظية
        # =====================================================
        scheduled_events = self.events_data.get("scheduled_events", [])
        current_events = self.events_data.get("events_current", [])

        await self.check_scheduled_events(channel, scheduled_events + current_events, now_utc)

        # تنظيف ذاكرة التنبيهات
        if len(self.sent_alerts) > 500:
            self.sent_alerts.clear()
        if len(self.sent_daily_resets) > 100:
            self.sent_daily_resets.clear()

    # ---------------------------------------------------------
    # فحص الأحداث المجدولة لحظة البدء/الانتهاء exact-minute
    # ---------------------------------------------------------
    async def check_scheduled_events(self, channel, events, now_utc):
        now_minute = now_utc.replace(second=0, microsecond=0)

        for event in events:
            if not event.get("enabled", True):
                continue

            start_time = self.parse_iso_time(event.get("start_time_iso"))
            end_time = self.parse_iso_time(event.get("end_time_iso"))

            if not start_time or not end_time:
                continue

            current_minute_key = now_minute.strftime("%Y%m%d%H%M")

            if now_minute == start_time:
                alert_key = f"{event['id']}_start_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "بدء الحدث • Event Started 🟢",
                        "بدأ الحدث الآن! • Started now!",
                        discord.Color.green(),
                    )
                    self.sent_alerts.add(alert_key)

            elif now_minute == end_time:
                alert_key = f"{event['id']}_end_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "انتهاء الحدث • Event Ended 🔴",
                        "انتهى الحدث الآن. • Ended now.",
                        discord.Color.red(),
                    )
                    self.sent_alerts.add(alert_key)

    # ---------------------------------------------------------
    # إرسال ملخص بداية اليوم الجديد المدمج
    # ---------------------------------------------------------
    async def send_daily_reset_summary(self, channel, now_utc, last_reset):
        embed = discord.Embed(
            title="🌅 يوم جديد في سكاي • New Sky Day",
            description="تم تجديد المهام اليومية والشموع! • Daily quests and candles reset!",
            color=discord.Color.purple(),
            timestamp=now_utc,
        )

        # 1. تجدد تماثيل إيدن (الأحد فقط)
        if now_utc.strftime("%A") == "Sunday":
            for rotation in self.events_data.get("weekly_rotations", []):
                if rotation.get("id") == "eden_reset":
                    name_ar = rotation.get("name_ar", "تجدد تماثيل إيدن")
                    name_en = rotation.get("name_en", "Eden Reset")
                    icon = rotation.get("icon", "💎")
                    embed.add_field(
                        name=f"{icon} {name_ar} • {name_en}",
                        value="تم إعادة ضبط تماثيل إيدن للأسبوع الجديد! 🔄\nEden statues have been reset for the week!",
                        inline=False,
                    )

        # 2. فحص الأرواح والفعاليات
        all_events = self.events_data.get("scheduled_events", []) + self.events_data.get("events_current", [])

        for event in all_events:
            if not event.get("enabled", True):
                continue

            start_time = self.parse_iso_time(event.get("start_time_iso"))
            end_time = self.parse_iso_time(event.get("end_time_iso"))

            if not start_time or not end_time:
                continue

            icon = event.get("icon", "✨")
            name_ar = event.get("name_ar", "حدث")
            name_en = event.get("name_en", "Event")
            realm = event.get("realm", "غير محدد")
            area = event.get("area", "غير محدد")

            # حالة الحدث
            if now_utc < start_time:
                start_in_seconds = (start_time - now_utc).total_seconds()
                days_to_start = int(start_in_seconds // 86400)
                time_str = f"يبدأ خلال {days_to_start} أيام • Starts in {days_to_start} days" if days_to_start > 0 else "يبدأ اليوم • Starts today"
                embed.add_field(
                    name=f"{icon} {name_ar} • {name_en} [قريباً • Soon]",
                    value=f"📍 {realm} - {area}\n⏳ {time_str}",
                    inline=False,
                )

            elif start_time <= now_utc < end_time:
                remaining_seconds = (end_time - now_utc).total_seconds()
                remaining_days = int(remaining_seconds // 86400)

                if remaining_days <= 0:
                    days_text = "ينتهي اليوم • Ends today"
                elif remaining_days == 1:
                    days_text = "متبقي يوم واحد • 1 day left"
                else:
                    days_text = f"متبقي {remaining_days} أيام • {remaining_days} days left"

                embed.add_field(
                    name=f"{icon} {name_ar} • {name_en} [مستمر • Active]",
                    value=f"📍 {realm} - {area}\n⏳ {days_text}",
                    inline=False,
                )

        await channel.send(embed=embed)

    # ---------------------------------------------------------
    # تحويل ISO إلى datetime
    # ---------------------------------------------------------
    def parse_iso_time(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                second=0, microsecond=0
            )
        except (ValueError, TypeError):
            return None

    # ---------------------------------------------------------
    # بناء وإرسال Embed التنبيهات الدورية
    # ---------------------------------------------------------
    async def send_event_embed(self, channel, event, status_title, description, color):
        name_ar = event.get("name_ar", "حدث")
        name_en = event.get("name_en", "Event")

        embed = discord.Embed(
            title=f"{event.get('icon', '✨')} {name_ar} • {name_en} — [{status_title}]",
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="🌍 العالم (Realm)",
            value=event.get("realm", "غير محدد"),
            inline=True,
        )

        embed.add_field(
            name="📍 المنطقة (Area)",
            value=event.get("area", "غير محدد"),
            inline=True,
        )

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(SkyEvents(bot))
            #🌋    
