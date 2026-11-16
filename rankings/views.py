from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Q
from django.db.models import F, Sum
from .models import Category, Dish, PlacesUsage, ToEatItem, get_uncategorized, Profile
from .serializers import (
    CategorySerializer, DishSerializer, ProfileSerializer, SearchResultSerializer, ToEatSerializer
)
from rest_framework import viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, action, permission_classes, parser_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.throttling import AnonRateThrottle
from . import placing
import os
import requests
import uuid
from datetime import timedelta
from django.utils import timezone
from urllib.parse import quote
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

# Create your views here.
@api_view(["GET"])
def hello(request):
    return Response({"message": "Food ranker API is alive"})

class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer

    def get_queryset(self):
        return Category.objects.filter(owner=self.request.user)

    def list(self, request, *args, **kwargs):
        get_uncategorized(request.user)
        return super().list(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if self.get_object().is_uncategorized:
            return Response({"error": "Uncategorized can't be deleted."}, status=400)
        return super().destroy(request, *args, **kwargs)

    def perform_create(self, serializer):
        last = (
            Category.objects.filter(owner=self.request.user, is_uncategorized=False)
            .order_by("-position")
            .first()
        )
        serializer.save(owner=self.request.user, position=(last.position + 1) if last else 0)

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        order = request.data.get("order", [])
        if not isinstance(order, list):
            return Response({"error": "order must be a list of category ids"}, status=400)
        for position, category_id in enumerate(order):
            self.get_queryset().filter(pk=category_id, is_uncategorized=False).update(position=position)
        return Response({"ok": True})

    @action(detail=False, methods=["get", "post"])
    def uncategorized(self, request):
        category = get_uncategorized(request.user)
        return Response(CategorySerializer(category, context={"request": request}).data)

class DishViewSet(viewsets.ModelViewSet):
    serializer_class = DishSerializer

    def get_queryset(self):
        dishes = Dish.objects.filter(category__owner=self.request.user)
        category = self.request.query_params.get("category")
        if category:
            dishes = dishes.filter(category_id=category)
        return dishes

    def perform_create(self, serializer):
        category = serializer.validated_data["category"]
        extra = {"elo": placing.bottom_score(category)} if category.is_uncategorized else {}
        serializer.save(**extra)
        if category.is_uncategorized:
            placing.record_ranks(category)
        to_eat_id = self.request.data.get("to_eat")
        if to_eat_id:
            ToEatItem.objects.filter(pk=to_eat_id, owner=self.request.user).delete()

    def perform_update(self, serializer):
        old_category = serializer.instance.category
        dish = serializer.save()
        if dish.category_id != old_category.id:
            if dish.category.is_uncategorized:
                dish.elo = placing.bottom_score(dish.category, exclude_pk=dish.pk)
            else:
                dish.elo = 1500.0
            dish.save(update_fields=["elo"])
            dish.history.all().delete()
            placing.record_ranks(old_category)
    
    def perform_destroy(self, instance):
        category = instance.category
        instance.delete()
        placing.record_ranks(category)
        
    @action(detail=True, methods=["post"])
    def place(self, request, pk=None):
        dish = self.get_object()
        answers = request.data.get("answers", [])
        if not isinstance(answers, list) or any(a not in ("win", "lose", "tie") for a in answers):
            return Response({"error": "answers must be a list of win, lose or tie"}, status=400)

        others = list(Dish.objects.filter(category=dish.category).exclude(pk=dish.pk))
        step, value = placing.next_step(others, answers)

        if step == "ask":
            opponent = DishSerializer(others[value], context={"request": request}).data
            return Response({"done": False, "round": len(answers) + 1, "opponent": opponent})

        dish.elo = placing.score_for_position(others, value)
        dish.save()
        placing.record_ranks(dish.category)
        return Response({
            "done": True,
            "rank": value + 1,
            "total": len(others) + 1,
            "dish": DishSerializer(dish, context={"request": request}).data,
        })

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        dish = self.get_object()
        try:
            position = int(request.data.get("position"))
        except (TypeError, ValueError):
            return Response({"error": "position must be a number"}, status=400)
        placing.move_dish(dish, position)
        dish.refresh_from_db()
        return Response(DishSerializer(dish, context={"request": request}).data)

    @action(detail=False, methods=["post"])
    def order(self, request):
        category = Category.objects.filter(pk=request.data.get("category"), owner=request.user).first()
        ids = request.data.get("order", [])
        if not category or not isinstance(ids, list):
            return Response({"error": "category and order are required"}, status=400)
        if set(ids) != set(category.dishes.values_list("id", flat=True)):
            return Response({"error": "order must list every dish in the category"}, status=400)
        placing.apply_order(category, ids)
        return Response({"ok": True})

def get_profile(user):
    return Profile.objects.get_or_create(user=user)[0]

def photo_url(profile):
    if profile.photo:
        return profile.photo.url
    if profile.use_google_photo and profile.google_photo:
        return profile.google_photo
    return None

def user_data(user):
    profile = get_profile(user)
    return {
        "id": user.id,
        "email": "" if profile.is_guest else user.username,
        "display_name": user.first_name,
        "photo": photo_url(profile),
        "welcomed": profile.welcomed,
        "is_guest": profile.is_guest,
    }

@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    if request.method == "PATCH":
        serializer = ProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.first_name = serializer.validated_data["display_name"]
        request.user.save(update_fields=["first_name"])
    return Response(user_data(request.user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_view(request):
    if get_profile(request.user).is_guest:
        request.user.delete()
    else:
        Token.objects.filter(user=request.user).delete()
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return Response({"ok": True})

@api_view(["GET"])
def search(request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return Response({"categories": [], "dishes": []})
    categories = Category.objects.filter(owner=request.user, name__icontains=q)[:10]
    dishes = (
        Dish.objects.filter(category__owner=request.user)
        .filter(Q(restaurant__icontains=q) | Q(name__icontains=q) | Q(note__icontains=q))
        .select_related("category")[:50]
    )
    context = {"request": request, "q": q}
    return Response({
        "categories": CategorySerializer(categories, many=True, context=context).data,
        "dishes": SearchResultSerializer(dishes, many=True, context=context).data,
    })

PLACES_URL = "https://places.googleapis.com/v1/places:autocomplete"

def allow_lookup():
    daily = int(os.environ.get("PLACES_DAILY_LIMIT", "400"))
    monthly = int(os.environ.get("PLACES_MONTHLY_LIMIT", "9000"))
    today = timezone.localdate()
    month_total = (
        PlacesUsage.objects.filter(day__year=today.year, day__month=today.month)
        .aggregate(total=Sum("count"))["total"] or 0
    )
    if month_total >= monthly:
        return False
    usage, _ = PlacesUsage.objects.get_or_create(day=today)
    updated = PlacesUsage.objects.filter(pk=usage.pk, count__lt=daily).update(count=F("count") + 1)
    return updated == 1

def maps_link(name, place_id):
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}&query_place_id={place_id}"

@api_view(["GET"])
def place_suggestions(request):
    q = request.query_params.get("q", "").strip()
    if len(q) < 3:
        return Response([])

    if request.query_params.get("source") != "google":
        rows = (
            Dish.objects.filter(category__owner=request.user, restaurant__icontains=q)
            .order_by("restaurant")
            .values("restaurant", "place_id", "map_link")
            .distinct()
        )
        entries = (
            ToEatItem.objects.filter(owner=request.user, restaurant__icontains=q)
            .order_by("restaurant")
            .values("restaurant", "place_id", "map_link")
            .distinct()
        )
        seen = {}
        for row in list(rows) + list(entries):
            name = row["restaurant"]
            link = row["map_link"] or (maps_link(name, row["place_id"]) if row["place_id"] else "")
            current = seen.get(name.lower())
            if current is None or (link and not current["map_link"]):
                seen[name.lower()] = {
                    "place_id": row["place_id"], "map_link": link, "name": name,
                    "address": "", "saved": True,
                }
        if seen:
            return Response(list(seen.values())[:5])

    if get_profile(request.user).is_guest:
        return Response([])

    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or not allow_lookup():
        return Response([])

    try:
        res = requests.post(
            PLACES_URL,
            json={
                "input": q,
                "languageCode": "en",
                "includedRegionCodes": ["in"],
                "includedPrimaryTypes": ["restaurant", "cafe", "bakery", "meal_takeaway", "fast_food_restaurant"],
            },
            headers={"X-Goog-Api-Key": key},
            timeout=5,
        )
        res.raise_for_status()
    except requests.RequestException:
        return Response([])

    items = []
    for suggestion in res.json().get("suggestions", []):
        prediction = suggestion.get("placePrediction")
        if not prediction:
            continue
        text = prediction.get("structuredFormat", {})
        name = text.get("mainText", {}).get("text", "")
        place_id = prediction.get("placeId", "")
        items.append({
            "place_id": place_id,
            "map_link": maps_link(name, place_id),
            "name": name,
            "address": text.get("secondaryText", {}).get("text", ""),
            "saved": False,
        })
    return Response(items[:5])

class ToEatViewSet(viewsets.ModelViewSet):
    serializer_class = ToEatSerializer

    def get_queryset(self):
        return ToEatItem.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        last = self.get_queryset().order_by("-position").first()
        serializer.save(owner=self.request.user, position=(last.position + 1) if last else 0)

    @action(detail=False, methods=["post"])
    def order(self, request):
        ids = request.data.get("order", [])
        items = list(self.get_queryset())
        if not isinstance(ids, list) or set(ids) != {item.id for item in items}:
            return Response({"error": "order must list every entry"}, status=400)
        for position, item_id in enumerate(ids):
            ToEatItem.objects.filter(pk=item_id, owner=request.user).update(position=position)
        return Response({"ok": True})

@api_view(["POST"])
@permission_classes([AllowAny])
def google_login(request):
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    credential = request.data.get("id_token", "")
    if not client_id:
        return Response({"error": "Google sign-in isn't set up on the server."}, status=500)
    if not credential:
        return Response({"error": "Missing Google token."}, status=400)
    try:
        info = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id, clock_skew_in_seconds=10
        )
    except ValueError:
        return Response({"error": "Could not verify your Google account."}, status=400)

    email = (info.get("email") or "").strip().lower()
    if not email or not info.get("email_verified"):
        return Response({"error": "Your Google email isn't verified."}, status=400)

    existing = User.objects.filter(username=email).first() or User.objects.filter(email__iexact=email).first()
    if existing is not None and not existing.is_active:
        return Response({"error": "This account is disabled."}, status=400)

    guest_user = None
    guest_key = request.data.get("guest_token")
    if guest_key:
        guest_token = Token.objects.filter(key=guest_key).select_related("user").first()
        if guest_token and get_profile(guest_token.user).is_guest:
            guest_user = guest_token.user

    name = (info.get("name") or email.split("@")[0]).strip()[:50]
    is_new = False

    if existing is None and guest_user is not None:
        user = guest_user
        user.username = email
        user.email = email
        if user.first_name in ("", "Guest"):
            user.first_name = name
        user.save()
        is_new = True
    elif existing is None:
        user = User.objects.create_user(username=email, email=email, first_name=name)
        is_new = True
    else:
        user = existing
        if guest_user is not None and guest_user.pk != user.pk:
            guest_user.delete()

    profile = get_profile(user)
    if is_new:
        profile.is_guest = False
        profile.welcomed = False
        profile.use_google_photo = True
    picture = (info.get("picture") or "").replace("=s96-c", "=s256-c")
    if picture:
        profile.google_photo = picture
    profile.save()

    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": user_data(user)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def delete_summary(request):
    dishes = Dish.objects.filter(category__owner=request.user)
    return Response({
        "categories": Category.objects.filter(owner=request.user, is_uncategorized=False).count(),
        "dishes": dishes.count(),
        "photos": dishes.exclude(photo="").exclude(photo__isnull=True).count(),
        "to_eat": ToEatItem.objects.filter(owner=request.user).count(),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_me(request):
    if request.data.get("confirm") is not True:
        return Response({"error": "Confirmation is required."}, status=400)
    request.user.delete()
    return Response({"ok": True})

@api_view(["POST", "DELETE"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
def profile_photo(request):
    profile = get_profile(request.user)
    if request.method == "POST":
        upload = request.FILES.get("photo")
        if not upload:
            return Response({"error": "No photo received."}, status=400)
        if upload.size > 5 * 1024 * 1024:
            return Response({"error": "That photo is too large."}, status=400)
        if profile.photo:
            profile.photo.delete(save=False)
        profile.photo = upload
        profile.save()
    else:
        if profile.photo:
            profile.photo.delete(save=False)
        profile.photo = None
        profile.use_google_photo = False
        profile.save()
    return Response(user_data(request.user))

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def welcome_done(request):
    name = (request.data.get("display_name") or "").strip()
    if name:
        if len(name) > 50:
            return Response({"display_name": ["Keep it under 50 characters."]}, status=400)
        request.user.first_name = name
        request.user.save(update_fields=["first_name"])
    profile = get_profile(request.user)
    profile.welcomed = True
    profile.save(update_fields=["welcomed"])
    return Response(user_data(request.user))

MAX_GUESTS = 300
GUEST_LIFETIME_DAYS = 30


class GuestThrottle(AnonRateThrottle):
    scope = "guest"


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GuestThrottle])
def guest_login(request):
    cutoff = timezone.now() - timedelta(days=GUEST_LIFETIME_DAYS)
    for old in User.objects.filter(profile__is_guest=True, date_joined__lt=cutoff)[:20]:
        old.delete()
    if Profile.objects.filter(is_guest=True).count() >= MAX_GUESTS:
        return Response(
            {"error": "Guest mode is busy right now. Try again later, or sign in with Google."}, status=503
        )
    user = User.objects.create_user(username=f"guest-{uuid.uuid4().hex[:16]}", first_name="Guest")
    profile = get_profile(user)
    profile.is_guest = True
    profile.welcomed = True
    profile.use_google_photo = False
    profile.save()
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": user_data(user)}, status=201)