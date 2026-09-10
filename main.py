import json
import os
import re
import asyncio
import uuid
from datetime import datetime
import discord
import firebase_admin
from firebase_admin import credentials, db
from deep_translator import GoogleTranslator, MyMemoryTranslator, LingueeTranslator
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive
from locales import TRANSLATIONS, get_text

# ==========================
# إعداد الثوابت والإعدادات
# ==========================
REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "0"))

SUPPORTED_LANGUAGES = ["ar", "en", "es", "ja", "ko", "bg", "vi"]

GENDER_ROLES = ["♂️", "♀️"]
AGE_ROLES = ["10 - 15", "16 - 20", "21 - 25", "26 - 30", "31 - 40", "40+"]
COUNTRY_CODES = [
    "🇾🇪", "🇸🇦", "🇪🇬", "🇩🇿", "🇵🇸", "🇦🇪", "🇮🇶", "🇲🇦", "🇹🇳", "🇯🇴",
    "🇺🇸", "🇪🇸", "🇹🇷", "🇰🇷", "🇯🇵", "🇩🇪", "🇫🇷", "🇬🇧", "🇷🇺", "🇨🇳", "🇧🇬", "🇻🇳", "🌐", "OTHER"
]
COUNTRY_ROLES = [code for code in COUNTRY_CODES if code != "OTHER"]

# =================================
# إعداد Firebase Realtime Database
# =================================
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_CREDENTIALS_RAW = os.getenv("FIREBASE_CREDENTIALS")

if FIREBASE_CREDENTIALS_RAW and FIREBASE_DB_URL:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_RAW)
        if "private_key" in cred_dict:
            cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")

        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
        print("✅ Connected to Firebase Realtime Database successfully!")
    except Exception as e:
        print(f"❌ Firebase initialization error: {e}")
else:
    print("⚠️ FIREBASE_CREDENTIALS or FIREBASE_DB_URL not found!")

# =============
# إعداد البوت
# =============
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- إدارة البيانات عن طريق Firebase ---
def load_user_profiles():
    try:
        ref = db.reference("user_profiles")
        data = ref.get()
        return data if data else {}
    except Exception as e:
        print(f"❌ Error fetching Firebase data: {e}")
        return {}

def get_user_profile(user_id):
    try:
        ref = db.reference(f"user_profiles/{user_id}")
        data = ref.get()
        return data if data else {
            "gender": "Not Set",
            "age": "Not Set",
            "country": "Not Set",
            "language": "en",
        }
    except Exception as e:
        print(f"❌ Error fetching profile for user {user_id}: {e}")
        return {
            "gender": "Not Set",
            "age": "Not Set",
            "country": "Not Set",
            "language": "en",
        }

def update_user_field(user_id, field, value):
    try:
        user_str = str(user_id)
        ref = db.reference(f"user_profiles/{user_str}")
        user_data = ref.get()
        
        if not user_data:
            user_data = {
                "gender": "Not Set",
                "age": "Not Set",
                "country": "Not Set",
                "language": "en",
            }
        
        user_data[field] = value
        ref.set(user_data)
    except Exception as e:
        print(f"❌ Error updating Firebase data: {e}")

# --- دمج الوظائف المساعدة للطلبات ---
def has_pending_request(user_id: int, req_type: str) -> bool:
    try:
        ref = db.reference("requests")
        snapshot = ref.order_by_child("user_id").equal_to(user_id).get()
        if snapshot:
            for item in snapshot.values():
                if item.get("type") == req_type and item.get("status") == "pending":
                    return True
        return False
    except Exception as e:
        print(f"❌ Error checking pending request: {e}")
        return False

def create_request_record(req_data: dict):
    try:
        ref = db.reference(f"requests/{req_data['request_id']}")
        ref.set(req_data)
    except Exception as e:
        print(f"❌ Error creating request in Firebase: {e}")

def update_request_status(req_id: str, updates: dict):
    try:
        ref = db.reference(f"requests/{req_id}")
        ref.update(updates)
    except Exception as e:
        print(f"❌ Error updating request status: {e}")

def get_request_record(req_id: str):
    try:
        ref = db.reference(f"requests/{req_id}")
        return ref.get()
    except Exception as e:
        print(f"❌ Error getting request record: {e}")
        return None

# --- ترجمة النصوص الذكية (محدثة) ---
async def translate_smart_preserve_format(text: str, target_lang: str) -> str:

    if not text or not text.strip():
        return text

    target_lang = target_lang.lower().strip()

    # ---------------------------------------------------------
    # فحص ما إذا كانت النتيجة عبارة عن رسالة خطأ وليست ترجمة
    # ---------------------------------------------------------
    def is_invalid_translation(result: str) -> bool:
        if not result or not isinstance(result, str):
            return True

        cleaned = result.strip().lower()

        if not cleaned:
            return True

        error_patterns = [
            "error 500",
            "error 400",
            "error 401",
            "error 403",
            "error 404",
            "error 429",
            "server error",
            "that's an error",
            "there was an error",
            "please try again later",
            "translation unavailable",
            "internal server error",
            "service unavailable",
            "temporarily unavailable",
            "too many requests",
            "bad request",
        ]

        return any(pattern in cleaned for pattern in error_patterns)

    # ---------------------------------------------------------
    # المحاولة 1: Google Translator
    # ---------------------------------------------------------
    try:
        translated = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: GoogleTranslator(
                    source="auto",
                    target=target_lang
                ).translate(text)
            ),
            timeout=15
        )

        if not is_invalid_translation(translated):
            return translated.strip()

        print("⚠️ Google returned an invalid/error translation. Trying MyMemory...")

    except Exception as e:
        print(f"⚠️ Google Translator failed: {e}. Trying MyMemory...")

    # ---------------------------------------------------------
    # انتظار بسيط قبل المصدر الثاني
    # ---------------------------------------------------------
    await asyncio.sleep(1)

    # ---------------------------------------------------------
    # المحاولة 2: MyMemory Translator
    # ---------------------------------------------------------
    try:
        translated = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: MyMemoryTranslator(
                    source="auto",
                    target=target_lang
                ).translate(text)
            ),
            timeout=15
        )

        if not is_invalid_translation(translated):
            return translated.strip()

        print("⚠️ MyMemory returned an invalid/error translation. Retrying Google...")

    except Exception as e:
        print(f"⚠️ MyMemory Translator failed: {e}. Retrying Google...")

    # ---------------------------------------------------------
    # انتظار بسيط قبل المحاولة الأخيرة
    # ---------------------------------------------------------
    await asyncio.sleep(1)

    # ---------------------------------------------------------
    # المحاولة 3: Google مرة أخرى
    # بدون إجبار source=en/ar
    # ---------------------------------------------------------
    try:
        translated = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: GoogleTranslator(
                    source="auto",
                    target=target_lang
                ).translate(text)
            ),
            timeout=15
        )

        if not is_invalid_translation(translated):
            return translated.strip()

        print("⚠️ Final Google attempt returned an invalid/error translation.")

    except Exception as e:
        print(f"⚠️ Final Google Translator attempt failed: {e}")

    # ---------------------------------------------------------
    # فشل جميع المحاولات
    # ---------------------------------------------------------
    return (
        "⚠️ **Translation Unavailable:** "
        "All translation services are temporarily unavailable. "
        "Please try again in a few seconds."
    )




# --- إدارة الرتب التلقائية ---
async def assign_profile_role(
    interaction: discord.Interaction,
    category_options: list,
    selected_role_name: str,
    role_color: discord.Color = discord.Color.default(),
):
    guild = interaction.guild

    if not guild:
        for g in interaction.client.guilds:
            member_in_g = g.get_member(interaction.user.id)
            if member_in_g:
                guild = g
                break

    if not guild:
        return

    member = guild.get_member(interaction.user.id)
    if not member:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except Exception:
            return

    roles_to_remove = [
        role
        for role in member.roles
        if role.name in category_options and role.name != selected_role_name
    ]
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove)
        except discord.Forbidden:
            pass

    target_role = discord.utils.get(guild.roles, name=selected_role_name)
    if not target_role:
        try:
            target_role = await guild.create_role(
                name=selected_role_name,
                color=role_color,
                reason="Auto-created profile role by Bot",
            )
        except discord.Forbidden:
            return

    if target_role not in member.roles:
        try:
            await member.add_roles(target_role)
        except discord.Forbidden:
            pass

# =======================
# Modals (نماذج الأدخال)
# =======================
class RequestLanguageModal(discord.ui.Modal):
    def __init__(self, user_lang: str):
        super().__init__(title=get_text(user_lang, "language_request_modal_title"))
        self.user_lang = user_lang
        self.requested_lang_input = discord.ui.TextInput(
            label=get_text(user_lang, "language_request_modal_label"),
            placeholder=get_text(user_lang, "language_request_modal_ph"),
            required=True,
            max_length=50
        )
        self.add_item(self.requested_lang_input)

    async def on_submit(self, interaction: discord.Interaction):
        req_id = f"lang_{uuid.uuid4().hex[:8]}"
        user = interaction.user
        requested_lang = self.requested_lang_input.value.strip()

        # تعيين اللغة مؤقتا لـ en
        update_user_field(user.id, "language", "en")
        active_lang = "en"

        req_data = {
            "request_id": req_id,
            "type": "language",
            "user_id": user.id,
            "user_name": str(user),
            "requested_value": requested_lang,
            "temp_lang": active_lang,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }
        create_request_record(req_data)

        await interaction.response.send_message(
            get_text(active_lang, "language_request_sent"), ephemeral=True
        )

        # إرسال إشعار لقناة الإدارة
        channel = interaction.client.get_channel(REQUEST_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=get_text(active_lang, "admin_lang_req_title"),
                color=discord.Color.gold()
            )
            embed.add_field(name=get_text(active_lang, "admin_field_member"), value=f"{user.mention} ({user.name})", inline=False)
            embed.add_field(name=get_text(active_lang, "admin_field_user_id"), value=str(user.id), inline=True)
            embed.add_field(name=get_text(active_lang, "admin_field_req_lang"), value=requested_lang, inline=True)
            embed.add_field(name=get_text(active_lang, "admin_field_curr_lang"), value="English", inline=True)
            embed.add_field(name=get_text(active_lang, "admin_field_status"), value=get_text(active_lang, "status_pending_badge"), inline=False)
            
            await channel.send(embed=embed, view=AdminRequestView(req_id, "language"))


class RequestCountryModal(discord.ui.Modal):
    def __init__(self, user_lang: str):
        super().__init__(title=get_text(user_lang, "country_request_modal_title"))
        self.user_lang = user_lang
        self.requested_country_input = discord.ui.TextInput(
            label=get_text(user_lang, "country_request_modal_label"),
            placeholder=get_text(user_lang, "country_request_modal_ph"),
            required=True,
            max_length=60
        )
        self.add_item(self.requested_country_input)

    async def on_submit(self, interaction: discord.Interaction):
        req_id = f"country_{uuid.uuid4().hex[:8]}"
        user = interaction.user
        requested_country = self.requested_country_input.value.strip()

        # تعيين الدولة مؤقتا لـ 🌐
        update_user_field(user.id, "country", "🌐")
        await assign_profile_role(interaction, COUNTRY_ROLES, "🌐", discord.Color.from_rgb(46, 204, 113))

        req_data = {
            "request_id": req_id,
            "type": "country",
            "user_id": user.id,
            "user_name": str(user),
            "requested_value": requested_country,
            "temp_country": "🌐",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }
        create_request_record(req_data)

        # الإشعار بلغة المستخدم الحالية
        await interaction.response.send_message(
            get_text(self.user_lang, "country_request_sent"), ephemeral=True
        )

        # إرسال إشعار لقناة الإدارة
        channel = interaction.client.get_channel(REQUEST_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=get_text(self.user_lang, "admin_country_req_title"),
                color=discord.Color.gold()
            )
            embed.add_field(name=get_text(self.user_lang, "admin_field_member"), value=f"{user.mention} ({user.name})", inline=False)
            embed.add_field(name=get_text(self.user_lang, "admin_field_user_id"), value=str(user.id), inline=True)
            embed.add_field(name=get_text(self.user_lang, "admin_field_req_country"), value=requested_country, inline=True)
            embed.add_field(name=get_text(self.user_lang, "admin_field_status"), value=get_text(self.user_lang, "status_pending_badge"), inline=False)
            
            await channel.send(embed=embed, view=AdminRequestView(req_id, "country"))


class RejectReasonModal(discord.ui.Modal):
    def __init__(self, req_id: str, req_type: str):
        super().__init__(title="Reject Request / سبب الرفض")
        self.req_id = req_id
        self.req_type = req_type
        self.reason_input = discord.ui.TextInput(
            label="Rejection Reason / سبب الرفض",
            style=discord.TextStyle.paragraph,
            placeholder="Enter reason here...",
            required=True,
            max_length=300
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason_input.value.strip()
        req = get_request_record(self.req_id)
        if not req or req.get("status") != "pending":
            await interaction.response.send_message("❌ Request already processed or invalid.", ephemeral=True)
            return

        resolved_time = datetime.utcnow().isoformat()
        update_request_status(self.req_id, {
            "status": "rejected",
            "rejection_reason": reason,
            "resolved_at": resolved_time
        })

        user_id = req["user_id"]
        user_prof = get_user_profile(user_id)
        u_lang = user_prof.get("language", "en")

        # تحديث Embed الإدارة
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_field_at(-1, name=get_text("en", "admin_field_status"), value=f"{get_text('en', 'status_rejected_badge')}\n**Reason:** {reason}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

        # مراسلة العضو بالسبب
        try:
            target_user = await interaction.client.fetch_user(user_id)
            if target_user:
                msg_key = "lang_req_rejected_dm" if self.req_type == "language" else "country_req_rejected_dm"
                base_msg = get_text(u_lang, msg_key)
                await target_user.send(f"{base_msg}\n**{get_text(u_lang, 'rejection_reason_label')}:** {reason}")
        except Exception as e:
            print(f"⚠️ Could not send DM to user: {e}")


class ApproveCountryModal(discord.ui.Modal):
    def __init__(self, req_id: str):
        super().__init__(title="Approve Country / قبول الدولة")
        self.req_id = req_id
        self.country_code_input = discord.ui.TextInput(
            label="Country Code / Emoji (e.g. 🇸🇾)",
            placeholder="Enter Emoji / Code",
            required=True,
            max_length=10
        )
        self.add_item(self.country_code_input)

    async def on_submit(self, interaction: discord.Interaction):
        country_code = self.country_code_input.value.strip()
        req = get_request_record(self.req_id)
        if not req or req.get("status") != "pending":
            await interaction.response.send_message("❌ Request already processed or invalid.", ephemeral=True)
            return

        user_id = req["user_id"]
        update_user_field(user_id, "country", country_code)

        # تحديث الرتب
        guild = interaction.guild
        if guild:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if member:
                temp_role = discord.utils.get(guild.roles, name="🌐")
                if temp_role and temp_role in member.roles:
                    try:
                        await member.remove_roles(temp_role)
                    except discord.Forbidden:
                        pass
                
                new_role = discord.utils.get(guild.roles, name=country_code)
                if not new_role:
                    try:
                        new_role = await guild.create_role(name=country_code, color=discord.Color.from_rgb(46, 204, 113))
                    except discord.Forbidden:
                        pass
                if new_role and new_role not in member.roles:
                    try:
                        await member.add_roles(new_role)
                    except discord.Forbidden:
                        pass

        resolved_time = datetime.utcnow().isoformat()
        update_request_status(self.req_id, {
            "status": "completed",
            "completed_at": resolved_time,
            "assigned_code": country_code
        })

        user_prof = get_user_profile(user_id)
        u_lang = user_prof.get("language", "en")

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_field_at(-1, name=get_text("en", "admin_field_status"), value=f"{get_text('en', 'status_completed_badge')} ({country_code})", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

        try:
            target_user = await interaction.client.fetch_user(user_id)
            if target_user:
                await target_user.send(get_text(u_lang, "country_req_approved_dm"))
        except Exception as e:
            print(f"⚠️ Could not send DM to user: {e}")

# =========================
# Views الإدارية والخيارات
# =========================
class AdminRequestView(discord.ui.View):
    def __init__(self, req_id: str, req_type: str):
        super().__init__(timeout=None)
        self.req_id = req_id
        self.req_type = req_type

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="admin_btn_approve")
    async def approve_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        req = get_request_record(self.req_id)
        if not req or req.get("status") != "pending":
            await interaction.response.send_message("❌ Request already processed or invalid.", ephemeral=True)
            return

        if self.req_type == "country":
            await interaction.response.send_modal(ApproveCountryModal(self.req_id))
        else:
            user_id = req["user_id"]
            req_value = req["requested_value"].lower()[:2]
            target_code = req_value if req_value in SUPPORTED_LANGUAGES else "en"

            update_user_field(user_id, "language", target_code)
            resolved_time = datetime.utcnow().isoformat()
            update_request_status(self.req_id, {
                "status": "completed",
                "completed_at": resolved_time,
                "assigned_code": target_code
            })

            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.set_field_at(-1, name="Status", value=f"✅ Approved ({target_code})", inline=False)
            await interaction.response.edit_message(embed=embed, view=None)

            try:
                target_user = await interaction.client.fetch_user(user_id)
                if target_user:
                    await target_user.send(get_text(target_code, "lang_req_approved_dm"))
            except Exception as e:
                print(f"⚠️ Could not send DM to user: {e}")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="admin_btn_reject")
    async def reject_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        req = get_request_record(self.req_id)
        if not req or req.get("status") != "pending":
            await interaction.response.send_message("❌ Request already processed or invalid.", ephemeral=True)
            return

        await interaction.response.send_modal(RejectReasonModal(self.req_id, self.req_type))


class GenderSelect(discord.ui.Select):
    def __init__(self, lang: str, current_val: str = None):
        self.lang = lang
        options = [
            discord.SelectOption(
                label=get_text(lang, "gender_m"), emoji="♂️", value="♂️", default=(current_val == "♂️")
            ),
            discord.SelectOption(
                label=get_text(lang, "gender_f"), emoji="♀️", value="♀️", default=(current_val == "♀️")
            ),
        ]
        super().__init__(
            placeholder=get_text(lang, "gender_ph"),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_gender = self.values[0]
        update_user_field(interaction.user.id, "gender", selected_gender)
        role_color = discord.Color.blue() if selected_gender == "♂️" else discord.Color.pink()

        await assign_profile_role(interaction, GENDER_ROLES, selected_gender, role_color)
        await interaction.response.send_message(
            get_text(self.lang, "saved_gender"), ephemeral=True
        )


class AgeSelect(discord.ui.Select):
    def __init__(self, lang: str, current_val: str = None):
        self.lang = lang
        options = [
            discord.SelectOption(
                label=age, value=age, default=(current_val == age)
            ) for age in AGE_ROLES
        ]
        super().__init__(
            placeholder=get_text(lang, "age_ph"),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_age = self.values[0]
        update_user_field(interaction.user.id, "age", selected_age)
        role_color = discord.Color.from_rgb(155, 89, 182)

        await assign_profile_role(interaction, AGE_ROLES, selected_age, role_color)
        await interaction.response.send_message(
            get_text(self.lang, "saved_age"), ephemeral=True
        )


class CountrySelect(discord.ui.Select):
    def __init__(self, lang: str, current_val: str = None):
        self.lang = lang
        target_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get("countries", {})
        
        options = [
            discord.SelectOption(
                label=target_dict.get(code, code) if code != "OTHER" else get_text(lang, "other_option_label"),
                emoji="🌐" if code == "OTHER" else code,
                value=code,
                default=(current_val == code)
            )
            for code in COUNTRY_CODES
        ]
        super().__init__(
            placeholder=get_text(lang, "country_ph"),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_country = self.values[0]

        if selected_country == "OTHER":
            if has_pending_request(interaction.user.id, "country"):
                await interaction.response.send_message(
                    get_text(self.lang, "country_request_pending"), ephemeral=True
                )
                return
            await interaction.response.send_modal(RequestCountryModal(self.lang))
            return

        update_user_field(interaction.user.id, "country", selected_country)
        role_color = discord.Color.from_rgb(46, 204, 113)

        await assign_profile_role(interaction, COUNTRY_ROLES, selected_country, role_color)
        await interaction.response.send_message(
            get_text(self.lang, "saved_country"), ephemeral=True
        )


class DetailsSurveyView(discord.ui.View):
    def __init__(self, lang: str, user_profile: dict = None):
        super().__init__(timeout=None)
        user_profile = user_profile or {}
        
        curr_gender = user_profile.get("gender")
        curr_age = user_profile.get("age")
        curr_country = user_profile.get("country")

        self.add_item(GenderSelect(lang, curr_gender))
        self.add_item(AgeSelect(lang, curr_age))
        self.add_item(CountrySelect(lang, curr_country))


class LanguageButtonView(discord.ui.View):
    def __init__(self, current_lang: str = None):
        super().__init__(timeout=None)
        if current_lang:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id == f"btn_{current_lang}":
                    item.style = discord.ButtonStyle.success

    async def handle_lang_click(self, interaction: discord.Interaction, lang_code: str):
        update_user_field(interaction.user.id, "language", lang_code)
        embed = discord.Embed(
            title=get_text(lang_code, "title"),
            description=get_text(lang_code, "survey_desc"),
            color=discord.Color.green(),
        )
        user_data = get_user_profile(interaction.user.id)
        await interaction.response.send_message(
            embed=embed, view=DetailsSurveyView(lang_code, user_data), ephemeral=True
        )

    @discord.ui.button(label="English", emoji="🇺🇸", style=discord.ButtonStyle.secondary, custom_id="btn_en")
    async def btn_en(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "en")

    @discord.ui.button(label="العربية", emoji="🇸🇦", style=discord.ButtonStyle.secondary, custom_id="btn_ar")
    async def btn_ar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "ar")

    @discord.ui.button(label="日本語", emoji="🇯🇵", style=discord.ButtonStyle.secondary, custom_id="btn_ja")
    async def btn_ja(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "ja")

    @discord.ui.button(label="Español", emoji="🇪🇸", style=discord.ButtonStyle.secondary, custom_id="btn_es")
    async def btn_es(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "es")

    @discord.ui.button(label="한국어", emoji="🇰🇷", style=discord.ButtonStyle.secondary, custom_id="btn_ko")
    async def btn_ko(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "ko")

    @discord.ui.button(label="Български", emoji="🇧🇬", style=discord.ButtonStyle.secondary, custom_id="btn_bg")
    async def btn_bg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "bg")

    @discord.ui.button(label="Tiếng Việt", emoji="🇻🇳", style=discord.ButtonStyle.secondary, custom_id="btn_vi")
    async def btn_vi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_lang_click(interaction, "vi")

    @discord.ui.button(label="Other / أخرى", emoji="🌐", style=discord.ButtonStyle.secondary, custom_id="btn_other_lang")
    async def btn_other_lang(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_prof = get_user_profile(interaction.user.id)
        current_user_lang = user_prof.get("language", "en")

        if has_pending_request(interaction.user.id, "language"):
            await interaction.response.send_message(
                get_text(current_user_lang, "language_request_pending"), ephemeral=True
            )
            return

        await interaction.response.send_modal(RequestLanguageModal(current_user_lang))


class ProfileManageView(discord.ui.View):
    def __init__(self, user_lang: str, user_data: dict = None):
        super().__init__(timeout=None)
        self.user_lang = user_lang
        self.user_data = user_data or {}
        
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "btn_edit_profile":
                    item.label = get_text(user_lang, "edit_btn")
                elif item.custom_id == "btn_change_lang":
                    item.label = get_text(user_lang, "change_lang_btn")

    @discord.ui.button(label="Edit Profile", style=discord.ButtonStyle.primary, custom_id="btn_edit_profile")
    async def edit_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=get_text(self.user_lang, "title"),
            description=get_text(self.user_lang, "survey_desc"),
            color=discord.Color.blue(),
        )
        user_data = get_user_profile(interaction.user.id)
        await interaction.response.send_message(
            embed=embed, view=DetailsSurveyView(self.user_lang, user_data), ephemeral=True
        )

    @discord.ui.button(label="Change Language", style=discord.ButtonStyle.secondary, custom_id="btn_change_lang")
    async def change_language(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=get_text(self.user_lang, "lang_title"),
            description=get_text(self.user_lang, "lang_desc"),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=embed, view=LanguageButtonView(current_lang=self.user_lang), ephemeral=True
        )

# ================
# الأحداث والأوامر
# ================
@bot.event
async def on_ready():
    # تحميل الكوج الخاص باللغات/الألعاب السابق
    try:
        await bot.load_extension("games")
    except Exception as e:
        print(f"⚠️ Extension 'games' load status: {e}")

    # تحميل كوج أحداث سكاي الجديد
    try:
        await bot.load_extension("sky_events")
        print("✅ SkyEvents Cog loaded successfully!")
    except Exception as e:
        print(f"⚠️ Extension 'sky_events' load status: {e}")

    bot.add_view(LanguageButtonView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user} & Slash commands synced!")


@bot.event
async def on_member_join(member: discord.Member):
    embed = discord.Embed(
        title="🌐 Choose Your Language",
        description="Select your preferred language to start setting up your profile & enable instant translation!",
        color=discord.Color.blue(),
    )
    try:
        await member.send(embed=embed, view=LanguageButtonView())
    except discord.Forbidden:
        pass


@bot.tree.command(name="profile", description="View & Edit your profile settings")
async def view_profile(interaction: discord.Interaction):
    user_data = get_user_profile(interaction.user.id)
    lang = user_data.get("language", "en")

    country_code = user_data.get("country", "Not Set")
    country_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get("countries", {})
    display_country = country_dict.get(
        country_code, 
        country_code if country_code != "Not Set" else get_text(lang, "not_set")
    )

    gender_val = user_data.get("gender", "Not Set")
    if gender_val == "♂️":
        display_gender = get_text(lang, "gender_m")
    elif gender_val == "♀️":
        display_gender = get_text(lang, "gender_f")
    else:
        display_gender = get_text(lang, "not_set")

    age_val = user_data.get("age", "Not Set")
    if age_val == "Not Set":
        age_val = get_text(lang, "not_set")

    embed = discord.Embed(
        title=get_text(lang, "profile_title"),
        description=get_text(lang, "profile_desc"),
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.add_field(name=f"🌐 {get_text(lang, 'lang_label')}", value=lang.upper(), inline=True)
    embed.add_field(name=f"👤 {get_text(lang, 'gender_label')}", value=display_gender, inline=True)
    embed.add_field(name=f"🎂 {get_text(lang, 'age_label')}", value=age_val, inline=True)
    embed.add_field(name=f"🚩 {get_text(lang, 'country_label')}", value=display_country, inline=True)

    await interaction.response.send_message(
        embed=embed, view=ProfileManageView(lang, user_data), ephemeral=True
    )


# --- الأمر النصي للترجمة بالرد (Prefix Command !t) ---
@bot.command(name="t")
async def quick_translate_prefix(ctx: commands.Context, target_language: str = None):
    user_info = get_user_profile(ctx.author.id)
    user_lang = user_info.get("language", "en")
    final_lang = target_language.lower().strip() if target_language else user_lang

    try:
        await ctx.message.delete()
    except Exception:
        pass

    if not ctx.message.reference or not ctx.message.reference.message_id:
        err = await ctx.send(get_text(user_lang, "reply_error"))
        await err.delete(delay=5)
        return

    try:
        target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
    except Exception as e:
        print(f"⚠️ Error fetching target message: {e}")
        return

    if not target_msg or not target_msg.content:
        err = await ctx.send(get_text(user_lang, "reply_error"))
        await err.delete(delay=5)
        return

    try:
        translated_text = await translate_smart_preserve_format(target_msg.content, final_lang)
        translated_msg = await ctx.send(f"🌐 **{final_lang.upper()}:**\n{translated_text}")
        await translated_msg.delete(delay=15)

    except Exception as e:
        err_msg = get_text(user_lang, "trans_error")
        err = await ctx.send(f"{err_msg} {e}")
        await err.delete(delay=5)

# --- أمر سياق الخيارات المباشر (Context Menu) ---
@bot.tree.context_menu(name="Translate to My Language")
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    user_info = get_user_profile(interaction.user.id)
    target_lang = user_info.get("language", "en")

    if not message.content:
        await interaction.response.send_message(
            get_text(target_lang, "no_text_error"), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        translated_text = await translate_smart_preserve_format(message.content, target_lang)
        await interaction.followup.send(translated_text, ephemeral=True)
    except Exception as e:
        err_msg = get_text(target_lang, "trans_error")
        await interaction.followup.send(f"{err_msg} {e}", ephemeral=True)

keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ DISCORD_TOKEN missing!")