import json
import os
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks


class SkyEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.json_path = "sky_events.json"
        self.shards_path = "shards_schedule.json"

        self.events_data = self.load_json(self.json_path)
        self.shards_data = self.load_json(self.shards_path)

        self.sent_alerts = set()
        self.sent_daily_resets = set()
        self.last_shard_message_id = None

        self.events_checker.start()

    def load_json(self, path):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def cog_unload(self):
        self.events_checker.cancel()

    # ---------------------------------------------------------
    # حساب وقت الريسيت اليومي الأخير بتوقيت السيرفر (UTC)
    # ---------------------------------------------------------
    def get_last_daily_reset_utc(self):
        now_utc = datetime.now(timezone.utc)
        reset_time_str = self.events_data.get("config", {}).get("daily_reset_utc", "07:00")
        reset_hour = int(reset_time_str.split(":")[0])

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

        channel_id = self.events_data.get("config", {}).get("channel_id") or int(
            os.getenv("SKY_EVENTS_CHANNEL_ID", "0")
        )

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
        # 1. إرسال ملخص بداية اليوم الجديد وتحديث قناة الثوران
        # =====================================================
        if minutes_since_reset <= 5:
            reset_key = last_reset.strftime("%Y%m%d")
            if reset_key not in self.sent_daily_resets:
                await self.update_shards_daily_channel(last_reset)
                await self.send_daily_reset_summary(channel, now_utc, last_reset)
                self.sent_daily_resets.add(reset_key)

        # =====================================================
        # 2. فحص تنبيهات جولات ثوران الشظايا اللحظية
        # =====================================================
        await self.check_shard_alerts(channel, minutes_since_reset, now_utc, last_reset)

        # =====================================================
        # 3. الأحداث الدورية - كل ساعتين (الجدة، الروضة، السلحفاة)
        # =====================================================
        for event in self.events_data.get("interval_events", []):
            repeat_hours = event.get("repeat_interval_hours", 2)
            cycle_minutes = repeat_hours * 60

            cycle_minute = minutes_since_reset % cycle_minutes

            alert_offset = event["alert_offset_minutes"]
            start_offset = event["start_offset_minutes"]
            end_offset = event["end_offset_minutes"]

            current_minute_key = now_minute.strftime("%Y%m%d%H%M")

            if cycle_minute == alert_offset:
                alert_key = f"{event['id']}_alert_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "Alert 🔔",
                        "سيبدأ الحدث بعد قليل! • Starting soon!",
                        discord.Color.gold(),
                    )
                    self.sent_alerts.add(alert_key)

            elif cycle_minute == start_offset:
                alert_key = f"{event['id']}_start_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "Start 🟢",
                        "بدأ الحدث الآن! • Started now!",
                        discord.Color.green(),
                    )
                    self.sent_alerts.add(alert_key)

            elif cycle_minute == (end_offset % cycle_minutes):
                alert_key = f"{event['id']}_end_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "End 🔴",
                        "انتهى الحدث الآن. • Ended now.",
                        discord.Color.red(),
                    )
                    self.sent_alerts.add(alert_key)

        # =====================================================
        # 4. الأحداث المجدولة (المستمرة، القادمة، أو الأرواح)
        # =====================================================
        all_scheduled = self.events_data.get("scheduled_events", []) + self.events_data.get("events_current", [])
        await self.check_scheduled_events(channel, all_scheduled, now_utc)

        # تنظيف ذاكرة التنبيهات
        if len(self.sent_alerts) > 500:
            self.sent_alerts.clear()
        if len(self.sent_daily_resets) > 100:
            self.sent_daily_resets.clear()

    # ---------------------------------------------------------
    # تحديث قناة الثوران اليومية معتمدة على يوم السيرفر
    # ---------------------------------------------------------
    async def update_shards_daily_channel(self, last_reset):
        shards_channel_id = self.shards_data.get("config", {}).get("shards_channel_id")
        if not shards_channel_id:
            return

        shards_channel = self.bot.get_channel(shards_channel_id)
        if not shards_channel:
            return

        try:
            await shards_channel.purge(limit=10)
        except Exception:
            pass

        day_str = str(last_reset.day)
        shard_info = self.shards_data.get("schedule", {}).get(day_str)

        if not shard_info:
            return

        weekday = last_reset.strftime("%A")
        no_shard = weekday in shard_info.get("no_shard_days", [])

        if no_shard:
            content = f"🌋 **الثوران اليومي** 🌋\n\n🗓️ **يوم السيرفر:** {day_str}\n\n❌ **لا يوجد ثوران لهذا اليوم** ({weekday})."
        else:
            days_translation = {
                "Saturday": "السبت", "Sunday": "الأحد", "Monday": "الإثنين",
                "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء", "Thursday": "الخميس", "Friday": "الجمعة"
            }
            no_days_ar = " و".join([days_translation.get(d, d) for d in shard_info.get("no_shard_days", [])])

            times_text = ""
            for window in shard_info.get("windows", []):
                start_dt = last_reset + timedelta(minutes=window["start_offset_minutes"])
                end_dt = last_reset + timedelta(minutes=window["end_offset_minutes"])
                
                start_ts = f"<t:{int(start_dt.timestamp())}:t>"
                end_ts = f"<t:{int(end_dt.timestamp())}:t>"
                times_text += f"• {start_ts} → {end_ts}\n"

            content = (
                f"🌋 **الثوران اليومي** 🌋\n\n"
                f"🗓️ **اليوم:** {day_str}\n\n"
                f"⚫ **النوع:** {shard_info.get('type_ar')}\n"
                f"🌍 **العالم:** {shard_info.get('realm')}\n"
                f"📍 **الموقع:** {shard_info.get('area')}\n"
                f"✨ **المكافأة:** {shard_info.get('rewards')}\n"
                f"🚫 **لا يظهر:** {no_days_ar}\n\n"
                f"⏰ **الأوقات:**\n{times_text}"
            )

        msg = await shards_channel.send(content)
        self.last_shard_message_id = msg.id

    # ---------------------------------------------------------
    # فحص وتنبيهات جولات ثوران الشظايا
    # ---------------------------------------------------------
    async def check_shard_alerts(self, channel, minutes_since_reset, now_utc, last_reset):
        day_str = str(last_reset.day)
        shard_info = self.shards_data.get("schedule", {}).get(day_str)

        if not shard_info:
            return

        weekday = last_reset.strftime("%A")
        if weekday in shard_info.get("no_shard_days", []):
            return

        now_minute_key = now_utc.strftime("%Y%m%d%H%M")

        for idx, window in enumerate(shard_info.get("windows", []), start=1):
            start_off = window["start_offset_minutes"]
            end_off = window["end_offset_minutes"]

            if minutes_since_reset == start_off:
                alert_key = f"shard_start_{last_reset.strftime('%Y%m%d')}_{idx}_{now_minute_key}"
                if alert_key not in self.sent_alerts:
                    embed = discord.Embed(
                        title="🌋 ثوران الشظايا │ 🟢 Start",
                        description="بدأ ثوران الشظايا الآن!",
                        color=discord.Color.green(),
                        timestamp=now_utc
                    )

                    embed.add_field(
                        name="🌍 العالم (Realm)",
                        value=shard_info.get("realm", "غير محدد"),
                        inline=True,
                    )

                    embed.add_field(
                        name="📍 المنطقة (Area)",
                        value=shard_info.get("area", "غير محدد"),
                        inline=True,
                    )

                    await channel.send(embed=embed)
                    self.sent_alerts.add(alert_key)

            elif minutes_since_reset == end_off:
                alert_key = f"shard_end_{last_reset.strftime('%Y%m%d')}_{idx}_{now_minute_key}"
                if alert_key not in self.sent_alerts:
                    embed = discord.Embed(
                        title=f"🌋 ثوران الشظايا — [End 🔴]",
                        description=f"انتهى ثوران الشظايا الآن.",
                        color=discord.Color.red(),
                        timestamp=now_utc
                    )
                    await channel.send(embed=embed)
                    self.sent_alerts.add(alert_key)

    # ---------------------------------------------------------
    # فحص الأحداث المجدولة بدون الحاجة لـ enabled
    # ---------------------------------------------------------
    async def check_scheduled_events(self, channel, events, now_utc):
        now_minute = now_utc.replace(second=0, microsecond=0)

        for event in events:
            start_time = self.parse_iso_time(event.get("start_time_iso"))
            end_time = self.parse_iso_time(event.get("end_time_iso"))

            if not start_time or not end_time:
                continue

            # تصفية الأحداث المنتهية كلياً
            if now_utc > end_time:
                continue

            current_minute_key = now_minute.strftime("%Y%m%d%H%M")

            if now_minute == start_time:
                alert_key = f"{event['id']}_start_{current_minute_key}"
                if alert_key not in self.sent_alerts:
                    await self.send_event_embed(
                        channel,
                        event,
                        "Start 🟢",
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
                        "End 🔴",
                        "انتهى الحدث الآن. • Ended now.",
                        discord.Color.red(),
                    )
                    self.sent_alerts.add(alert_key)

    # ---------------------------------------------------------
    # ملخص اليوم الجديد - يعتمد بالكامل على وقت البدء والنهاية
    # ---------------------------------------------------------
    async def send_daily_reset_summary(self, channel, now_utc, last_reset):
        embed = discord.Embed(
            title="🌅 يوم جديد في سكاي • New Sky Day",
            description="تم تجديد المهام اليومية والشموع! • Daily quests and candles reset!",
            color=discord.Color.purple(),
            timestamp=now_utc,
        )

        shards_channel_id = self.shards_data.get("config", {}).get("shards_channel_id")
        shards_mention = f"<#{shards_channel_id}>" if shards_channel_id else "قناة الثوران"

        day_str = str(last_reset.day)
        shard_info = self.shards_data.get("schedule", {}).get(day_str, {})
        weekday = last_reset.strftime("%A")

        if weekday in shard_info.get("no_shard_days", []):
            shard_status = "لا يوجد ثوران اليوم ❌"
        else:
            shard_status = f"يتوفر ثوران اليوم ({shard_info.get('type_ar')}) 🌋"

        embed.add_field(
            name="🌋 ثوران الشظايا • Shard Eruption",
            value=f"{shard_status}\nلمعرفة التفاصيل الكاملة والمواعيد راجع: {shards_mention}",
            inline=False,
        )

        if last_reset.strftime("%A") == "Sunday":
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

        # دمج كل الأحداث في قائمة واحدة للتقييم
        all_events = self.events_data.get("scheduled_events", []) + self.events_data.get("events_current", [])

        for event in all_events:
            start_time = self.parse_iso_time(event.get("start_time_iso"))
            end_time = self.parse_iso_time(event.get("end_time_iso"))

            if not start_time or not end_time:
                continue

            # التجاهل التلقائي للأحداث المنتهية
            if now_utc > end_time:
                continue

            icon = event.get("icon", "✨")
            name_ar = event.get("name_ar", "حدث")
            name_en = event.get("name_en", "Event")
            realm = event.get("realm", "غير محدد")
            area = event.get("area", "غير محدد")

            # 1. الحدث لم يبدأ بعد (حساب المتبقي لبدئه)
            if now_utc < start_time:
                time_until_start = start_time - now_utc
                days_left = time_until_start.days
                hours_left = int(time_until_start.seconds // 3600)

                if days_left > 0:
                    time_str = f"يبدأ خلال {days_left} يوم و {hours_left} ساعة • Starts in {days_left}d {hours_left}h"
                elif hours_left > 0:
                    time_str = f"يبدأ خلال {hours_left} ساعة • Starts in {hours_left}h"
                else:
                    time_str = "يبدأ خلال أقل من ساعة • Starts in less than an hour"

                embed.add_field(
                    name=f"{icon} {name_ar} • {name_en} [قريباً • Soon]",
                    value=f"📍 {realm} - {area}\n⏳ {time_str}",
                    inline=False,
                )

            # 2. الحدث نشط حالياً (حساب المتبقي لينتهي)
            elif start_time <= now_utc <= end_time:
                time_until_end = end_time - now_utc
                days_left = time_until_end.days
                hours_left = int(time_until_end.seconds // 3600)

                if days_left > 0:
                    time_str = f"متبقي {days_left} يوم و {hours_left} ساعة • Ends in {days_left}d {hours_left}h"
                elif hours_left > 0:
                    time_str = f"متبقي {hours_left} ساعة • Ends in {hours_left}h"
                else:
                    time_str = "ينتهي خلال أقل من ساعة • Ends in less than an hour"

                embed.add_field(
                    name=f"{icon} {name_ar} • {name_en} [مستمر • Active]",
                    value=f"📍 {realm} - {area}\n⏳ {time_str}",
                    inline=False,
                )

        await channel.send(embed=embed)

    def parse_iso_time(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                second=0, microsecond=0
            )
        except (ValueError, TypeError):
            return None

    async def send_event_embed(self, channel, event, status_title, description, color):
        name_ar = event.get("name_ar", "حدث")
        name_en = event.get("name_en", "Event")

        embed = discord.Embed(
            title=f"{event.get('icon', '✨')} {name_ar} • {name_en} │ {status_title}",
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