import os
from functools import wraps

from flask import (
    Flask, render_template, abort, request,
    redirect, url_for, session, flash, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import Markup
import psycopg
import markdown as md
from db import get_connection
from certificate import build_certificate

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("Добавь FLASK_SECRET_KEY в файл .env")


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Сначала войди в аккаунт.", "error")
            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped_view


@app.template_filter("markdown")
def markdown_filter(text):
    """Превращает текст урока в формате Markdown в HTML."""
    return Markup(md.markdown(text or "", extensions=["fenced_code", "tables"]))


# Средняя оценка и число отзывов по каждому курсу (отзыв привязан к записи на курс)
RATING_JOIN = """
    LEFT JOIN (
        SELECT e.course_id,
               ROUND(AVG(r.rating)::numeric, 1) AS avg_rating,
               COUNT(*) AS review_count
        FROM elearning_v2.reviews r
        JOIN elearning_v2.enrollments e ON e.enrollment_id = r.enrollment_id
        GROUP BY e.course_id
    ) rt ON rt.course_id = c.course_id
"""

PASS_SCORE = 60  # минимальный балл, чтобы тест считался сданным


def certificate_status(cur, student_id, course_id):
    """Можно ли выдать сертификат: курс завершён или все тесты курса сданы."""
    cur.execute("""
        SELECT
            e.status,
            (SELECT COUNT(*)
             FROM elearning_v2.quizzes q
             JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
             WHERE l.course_id = e.course_id) AS total_quizzes,
            (SELECT COUNT(DISTINCT qa.quiz_id)
             FROM elearning_v2.quiz_attempts qa
             JOIN elearning_v2.quizzes q ON q.quiz_id = qa.quiz_id
             JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
             WHERE l.course_id = e.course_id
               AND qa.student_id = e.student_id
               AND qa.score >= %(pass)s) AS passed_quizzes,
            (SELECT ROUND(AVG(best))
             FROM (SELECT MAX(qa.score) AS best
                   FROM elearning_v2.quiz_attempts qa
                   JOIN elearning_v2.quizzes q ON q.quiz_id = qa.quiz_id
                   JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
                   WHERE l.course_id = e.course_id
                     AND qa.student_id = e.student_id
                   GROUP BY qa.quiz_id) b) AS best_avg
        FROM elearning_v2.enrollments e
        WHERE e.student_id = %(s)s AND e.course_id = %(c)s
        ORDER BY (e.status = 'completed') DESC, e.enrollment_id
        LIMIT 1
    """, {"s": student_id, "c": course_id, "pass": PASS_SCORE})
    row = cur.fetchone()
    if row is None:
        return None
    status, total, passed, best_avg = row
    return {
        "enrolled": True,
        "completed": status == "completed",
        "total": total,
        "passed": passed,
        "best_avg": int(best_avg) if best_avg is not None else None,
        "eligible": status == "completed" or (total > 0 and passed >= total),
    }


@app.route("/")
def index():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM elearning_v2.courses),
                    (SELECT COUNT(*) FROM elearning_v2.lessons),
                    (SELECT COUNT(*) FROM elearning_v2.users),
                    (SELECT COUNT(*) FROM elearning_v2.categories)
            """)
            stats = cur.fetchone()

            cur.execute("""
                SELECT
                    c.course_id,
                    c.title,
                    c.price,
                    cat.name AS category,
                    u.full_name AS instructor,
                    rt.avg_rating,
                    COALESCE(rt.review_count, 0)
                FROM elearning_v2.courses c
                LEFT JOIN elearning_v2.categories cat
                    ON cat.category_id = c.category_id
                LEFT JOIN elearning_v2.users u
                    ON u.user_id = c.instructor_id
                """ + RATING_JOIN + """
                ORDER BY COALESCE(rt.review_count, 0) DESC,
                         rt.avg_rating DESC NULLS LAST,
                         c.course_id
                LIMIT 6
            """)
            courses = cur.fetchall()

    return render_template("index.html", stats=stats, courses=courses)


@app.route("/courses")
def course_list():
    # Получаем параметры поиска из URL (например, /courses?q=python&category=IT)
    search_query = request.args.get('q', '').strip()
    category_filter = request.args.get('category', '').strip()
    sort = request.args.get('sort', 'new')
    sort_options = {
        "new": "c.created_at DESC, c.course_id DESC",
        "rating": "rt.avg_rating DESC NULLS LAST, COALESCE(rt.review_count, 0) DESC",
        "popular": "students DESC, c.course_id",
        "price_asc": "c.price ASC",
        "price_desc": "c.price DESC",
    }
    if sort not in sort_options:
        sort = "new"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Получаем список всех категорий для выпадающего списка в фильтре
            cur.execute("""
                SELECT DISTINCT name 
                FROM elearning_v2.categories 
                WHERE name IS NOT NULL 
                ORDER BY name
            """)
            categories = [row[0] for row in cur.fetchall()]

            # 2. Формируем базовый запрос для курсов
            query = """
                SELECT
                    c.course_id, c.title, c.price, cat.name, u.full_name,
                    rt.avg_rating,
                    COALESCE(rt.review_count, 0),
                    (SELECT COUNT(*) FROM elearning_v2.enrollments e
                     WHERE e.course_id = c.course_id) AS students
                FROM elearning_v2.courses c
                LEFT JOIN elearning_v2.categories cat
                    ON cat.category_id = c.category_id
                LEFT JOIN elearning_v2.users u
                    ON u.user_id = c.instructor_id
            """ + RATING_JOIN + """
                WHERE 1=1
            """
            params = []

            # 3. Если есть поисковой запрос, добавляем условие (ILIKE - поиск без учета регистра)
            if search_query:
                query += " AND c.title ILIKE %s"
                params.append(f"%{search_query}%")
            
            # 4. Если выбрана категория, добавляем фильтр
            if category_filter:
                query += " AND cat.name = %s"
                params.append(category_filter)
                
            query += " ORDER BY " + sort_options[sort]
            
            # 5. Выполняем итоговый запрос
            cur.execute(query, tuple(params))
            courses = cur.fetchall()

    return render_template(
        "courses.html", 
        courses=courses,
        categories=categories, 
        search_query=search_query, 
        current_category=category_filter,
        current_sort=sort
    )


@app.route("/courses/<int:course_id>")
def course_detail(course_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.course_id, c.title, c.price, cat.name, u.full_name,
                       rt.avg_rating, COALESCE(rt.review_count, 0)
                FROM elearning_v2.courses c
                LEFT JOIN elearning_v2.categories cat
                    ON cat.category_id = c.category_id
                LEFT JOIN elearning_v2.users u
                    ON u.user_id = c.instructor_id
            """ + RATING_JOIN + """
                WHERE c.course_id = %s
            """, (course_id,))

            course = cur.fetchone()

            if course is None:
                abort(404)

            cur.execute("""
                SELECT lesson_id, lesson_order, title
                FROM elearning_v2.lessons
                WHERE course_id = %s
                ORDER BY lesson_order
            """, (course_id,))

            lessons = cur.fetchall()

            cur.execute("""
                SELECT r.rating, r.comment, r.created_at, u.full_name
                FROM elearning_v2.reviews r
                JOIN elearning_v2.enrollments e
                    ON e.enrollment_id = r.enrollment_id
                JOIN elearning_v2.users u
                    ON u.user_id = e.student_id
                WHERE e.course_id = %s
                ORDER BY r.created_at DESC, r.review_id DESC
                LIMIT 10
            """, (course_id,))
            course_reviews = cur.fetchall()

            is_enrolled = False
            my_review = None
            cert = None
            if "user_id" in session:
                cert = certificate_status(cur, session["user_id"], course_id)
                is_enrolled = cert is not None

                if is_enrolled:
                    cur.execute("""
                        SELECT r.rating, r.comment
                        FROM elearning_v2.reviews r
                        JOIN elearning_v2.enrollments e
                            ON e.enrollment_id = r.enrollment_id
                        WHERE e.student_id = %s AND e.course_id = %s
                        ORDER BY r.review_id DESC
                        LIMIT 1
                    """, (session["user_id"], course_id))
                    my_review = cur.fetchone()

    return render_template(
        "course_detail.html",
        course=course,
        lessons=lessons,
        is_enrolled=is_enrolled,
        course_reviews=course_reviews,
        my_review=my_review,
        cert=cert,
        pass_score=PASS_SCORE
    )


@app.route("/courses/<int:course_id>/review", methods=["POST"])
@login_required
def leave_review(course_id):
    try:
        rating = int(request.form.get("rating", ""))
    except ValueError:
        rating = 0
    comment = request.form.get("comment", "").strip()[:1000] or None

    if not 1 <= rating <= 5:
        flash("Поставь оценку от 1 до 5 звёзд.", "error")
        return redirect(url_for("course_detail", course_id=course_id) + "#reviews")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT enrollment_id, status FROM elearning_v2.enrollments
                WHERE student_id = %s AND course_id = %s
                ORDER BY (status = 'completed') DESC, enrollment_id
                LIMIT 1
            """, (session["user_id"], course_id))
            enrollment = cur.fetchone()

            if enrollment is None:
                flash("Оставлять отзывы могут только записанные на курс.", "error")
                return redirect(url_for("course_detail", course_id=course_id))

            enrollment_id, status = enrollment

            # Правило базы (триггер fn_check_review): отзыв — только после завершения курса
            if status != "completed":
                flash("Отзыв можно оставить после завершения курса — "
                      "сдай все тесты курса.", "error")
                return redirect(url_for("course_detail", course_id=course_id) + "#reviews")

            try:
                cur.execute("""
                    SELECT review_id FROM elearning_v2.reviews
                    WHERE enrollment_id = %s
                    ORDER BY review_id DESC LIMIT 1
                """, (enrollment_id,))
                existing = cur.fetchone()

                if existing:
                    cur.execute("""
                        UPDATE elearning_v2.reviews
                        SET rating = %s, comment = %s, created_at = CURRENT_DATE
                        WHERE review_id = %s
                    """, (rating, comment, existing[0]))
                    flash("Отзыв обновлён. Спасибо!", "success")
                else:
                    try:
                        cur.execute("""
                            INSERT INTO elearning_v2.reviews
                                (enrollment_id, rating, comment, created_at)
                            VALUES (%s, %s, %s, CURRENT_DATE)
                        """, (enrollment_id, rating, comment))
                    except psycopg.errors.NotNullViolation:
                        # у review_id нет автоматического счётчика — считаем номер сами
                        conn.rollback()
                        cur.execute("""
                            INSERT INTO elearning_v2.reviews
                                (review_id, enrollment_id, rating, comment, created_at)
                            SELECT COALESCE(MAX(review_id), 0) + 1, %s, %s, %s, CURRENT_DATE
                            FROM elearning_v2.reviews
                        """, (enrollment_id, rating, comment))
                    flash("Спасибо за отзыв!", "success")

            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                app.logger.exception("No privilege to write reviews")
                flash("Не удалось сохранить отзыв: у пользователя базы нет прав "
                      "на запись в таблицу reviews.", "error")
            except psycopg.errors.RaiseException as e:
                # отказ от проверки в самой базе (триггер)
                conn.rollback()
                flash("База данных не приняла отзыв: "
                      f"{e.diag.message_primary or e}", "error")

    return redirect(url_for("course_detail", course_id=course_id) + "#reviews")


@app.route("/certificates/<int:course_id>.pdf")
@login_required
def certificate(course_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cert = certificate_status(cur, session["user_id"], course_id)

            if cert is None or not cert["eligible"]:
                flash("Сертификат станет доступен, когда ты сдашь все тесты курса "
                      f"(от {PASS_SCORE}%).", "error")
                return redirect(url_for("course_detail", course_id=course_id))

            cur.execute("""
                SELECT c.title, u.full_name
                FROM elearning_v2.courses c
                LEFT JOIN elearning_v2.users u ON u.user_id = c.instructor_id
                WHERE c.course_id = %s
            """, (course_id,))
            course_title, instructor = cur.fetchone()

            cur.execute("""
                SELECT full_name FROM elearning_v2.users WHERE user_id = %s
            """, (session["user_id"],))
            student_name = cur.fetchone()[0]

    pdf = build_certificate(
        student_name=student_name,
        course_title=course_title,
        instructor=instructor,
        score=cert["best_avg"],
        number=f"LH-{course_id:03d}-{session['user_id']:05d}",
    )
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"certificate-course-{course_id}.pdf",
    )


@app.route("/courses/<int:course_id>/enroll", methods=["POST"])
@login_required
def enroll(course_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM elearning_v2.courses WHERE course_id = %s
            """, (course_id,))
            if cur.fetchone() is None:
                abort(404)

            cur.execute("""
                SELECT 1 FROM elearning_v2.enrollments
                WHERE student_id = %s AND course_id = %s
            """, (session["user_id"], course_id))

            if cur.fetchone() is None:
                cur.execute("""
                    INSERT INTO elearning_v2.enrollments
                        (student_id, course_id, status, enrolled_at)
                    VALUES (%s, %s, 'active', CURRENT_DATE)
                """, (session["user_id"], course_id))
                flash("Ты записан на курс! Он появился в разделе «Мои курсы».", "success")
            else:
                flash("Ты уже записан на этот курс.", "success")

    return redirect(url_for("course_detail", course_id=course_id))


@app.route("/my-courses")
@login_required
def my_courses():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.course_id,
                    c.title,
                    cat.name,
                    u.full_name,
                    e.status,
                    e.enrolled_at,
                    (SELECT COUNT(*) FROM elearning_v2.lessons l
                     WHERE l.course_id = c.course_id) AS lesson_count,
                    (SELECT ROUND(AVG(qa.score))
                     FROM elearning_v2.quiz_attempts qa
                     JOIN elearning_v2.quizzes q ON q.quiz_id = qa.quiz_id
                     JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
                     WHERE l.course_id = c.course_id
                       AND qa.student_id = e.student_id) AS avg_score,
                    (SELECT MIN(l.lesson_id) FROM elearning_v2.lessons l
                     WHERE l.course_id = c.course_id
                       AND l.lesson_order = (
                           SELECT MIN(lesson_order) FROM elearning_v2.lessons
                           WHERE course_id = c.course_id)) AS first_lesson_id,
                    (SELECT COUNT(*)
                     FROM elearning_v2.quizzes q
                     JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
                     WHERE l.course_id = c.course_id) AS total_quizzes,
                    (SELECT COUNT(DISTINCT qa.quiz_id)
                     FROM elearning_v2.quiz_attempts qa
                     JOIN elearning_v2.quizzes q ON q.quiz_id = qa.quiz_id
                     JOIN elearning_v2.lessons l ON l.lesson_id = q.lesson_id
                     WHERE l.course_id = c.course_id
                       AND qa.student_id = e.student_id
                       AND qa.score >= %(pass)s) AS passed_quizzes
                FROM elearning_v2.enrollments e
                JOIN elearning_v2.courses c ON c.course_id = e.course_id
                LEFT JOIN elearning_v2.categories cat
                    ON cat.category_id = c.category_id
                LEFT JOIN elearning_v2.users u
                    ON u.user_id = c.instructor_id
                WHERE e.student_id = %(s)s
                ORDER BY (e.status = 'completed'), e.enrolled_at DESC
            """, {"s": session["user_id"], "pass": PASS_SCORE})
            courses = cur.fetchall()

    active = sum(1 for c in courses if c[4] != "completed")
    completed = len(courses) - active

    return render_template(
        "my_courses.html",
        courses=courses,
        active=active,
        completed=completed
    )


@app.route("/lessons/<int:lesson_id>")
def lesson_detail(lesson_id):
    lesson_query = """
        SELECT
            l.lesson_id,
            l.lesson_order,
            l.title,
            c.course_id,
            c.title,
            {content}
        FROM elearning_v2.lessons l
        JOIN elearning_v2.courses c
            ON c.course_id = l.course_id
        WHERE l.lesson_id = %s
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(lesson_query.format(content="l.content"), (lesson_id,))
            except psycopg.errors.UndefinedColumn:
                # Колонку content ещё не добавили в базу (fill_lessons.sql не выполнен)
                conn.rollback()
                cur.execute(lesson_query.format(content="NULL"), (lesson_id,))

            lesson = cur.fetchone()

            if lesson is None:
                abort(404)

            # Соседние уроки этого курса — для кнопок «Назад» / «Далее»
            cur.execute("""
                SELECT
                    (SELECT lesson_id FROM elearning_v2.lessons
                     WHERE course_id = %(c)s AND lesson_order < %(o)s
                     ORDER BY lesson_order DESC LIMIT 1),
                    (SELECT lesson_id FROM elearning_v2.lessons
                     WHERE course_id = %(c)s AND lesson_order > %(o)s
                     ORDER BY lesson_order LIMIT 1),
                    (SELECT quiz_id FROM elearning_v2.quizzes
                     WHERE lesson_id = %(l)s LIMIT 1)
            """, {"c": lesson[3], "o": lesson[1], "l": lesson[0]})
            prev_id, next_id, quiz_id = cur.fetchone()

    return render_template(
        "lesson_detail.html",
        lesson=lesson,
        prev_id=prev_id,
        next_id=next_id,
        quiz_id=quiz_id
    )


@app.route("/quizzes")
def quiz_list():
    search_query = request.args.get("q", "").strip()
    pattern = f"%{search_query}%"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    q.quiz_id,
                    q.title,
                    l.title AS lesson_title,
                    c.title AS course_title,
                    COUNT(qq.question_id) AS question_count
                FROM elearning_v2.quizzes q
                JOIN elearning_v2.lessons l
                    ON l.lesson_id = q.lesson_id
                JOIN elearning_v2.courses c
                    ON c.course_id = l.course_id
                LEFT JOIN elearning_v2.quiz_questions qq
                    ON qq.quiz_id = q.quiz_id
                WHERE %s = ''
                   OR q.title ILIKE %s
                   OR l.title ILIKE %s
                   OR c.title ILIKE %s
                GROUP BY
                    q.quiz_id,
                    q.title,
                    l.title,
                    c.title
                ORDER BY q.quiz_id
            """, (search_query, pattern, pattern, pattern))
            quizzes = cur.fetchall()

    return render_template(
        "quizzes.html",
        quizzes=quizzes,
        search_query=search_query
    )


@app.route("/search")
def search():
    """Общий поиск по курсам, урокам и тестам."""
    search_query = request.args.get("q", "").strip()[:100]
    courses, lessons, quizzes = [], [], []

    if search_query:
        pattern = f"%{search_query}%"

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.course_id, c.title, c.price,
                           cat.name, u.full_name
                    FROM elearning_v2.courses c
                    LEFT JOIN elearning_v2.categories cat
                        ON cat.category_id = c.category_id
                    LEFT JOIN elearning_v2.users u
                        ON u.user_id = c.instructor_id
                    WHERE c.title ILIKE %s
                       OR cat.name ILIKE %s
                       OR u.full_name ILIKE %s
                    ORDER BY c.title
                    LIMIT 30
                """, (pattern, pattern, pattern))
                courses = cur.fetchall()

                cur.execute("""
                    SELECT l.lesson_id, l.title, l.lesson_order,
                           c.course_id, c.title
                    FROM elearning_v2.lessons l
                    JOIN elearning_v2.courses c
                        ON c.course_id = l.course_id
                    WHERE l.title ILIKE %s
                    ORDER BY c.title, l.lesson_order
                    LIMIT 30
                """, (pattern,))
                lessons = cur.fetchall()

                cur.execute("""
                    SELECT q.quiz_id, q.title, l.title, c.title
                    FROM elearning_v2.quizzes q
                    JOIN elearning_v2.lessons l
                        ON l.lesson_id = q.lesson_id
                    JOIN elearning_v2.courses c
                        ON c.course_id = l.course_id
                    WHERE q.title ILIKE %s
                       OR l.title ILIKE %s
                       OR c.title ILIKE %s
                    ORDER BY q.title
                    LIMIT 30
                """, (pattern, pattern, pattern))
                quizzes = cur.fetchall()

    return render_template(
        "search.html",
        search_query=search_query,
        courses=courses,
        lessons=lessons,
        quizzes=quizzes,
        total=len(courses) + len(lessons) + len(quizzes)
    )

@app.route("/quizzes/<int:quiz_id>", methods=["GET", "POST"])
@login_required
def take_quiz(quiz_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT quiz_id, title
                FROM elearning_v2.quizzes
                WHERE quiz_id = %s
            """, (quiz_id,))

            quiz = cur.fetchone()

            if quiz is None:
                abort(404)

            cur.execute("""
                SELECT l.course_id
                FROM elearning_v2.quizzes q
                JOIN elearning_v2.lessons l
                ON l.lesson_id = q.lesson_id
                WHERE q.quiz_id = %s
            """, (quiz_id,))

            course = cur.fetchone()

            if course is None:
                abort(404)

            cur.execute("""
                SELECT 1
                FROM elearning_v2.enrollments
                WHERE student_id = %s
                AND course_id = %s
                """, (session["user_id"], course[0]))

            if cur.fetchone() is None:
                cur.execute("""
                INSERT INTO elearning_v2.enrollments
                (student_id, course_id, status, enrolled_at)
                VALUES (%s, %s, 'active', CURRENT_DATE)
                """, (session["user_id"], course[0]))

            cur.execute("""
                SELECT question_id, question_text,
                       option_a, option_b, option_c, option_d
                FROM elearning_v2.quiz_questions
                WHERE quiz_id = %s
                ORDER BY question_order
            """, (quiz_id,))

            questions = cur.fetchall()

            if not questions:
                flash("В этом тесте пока нет вопросов.", "error")
                return redirect(url_for("quiz_list"))

            if request.method == "POST":
                cur.execute("""
                    SELECT question_id, correct_option
                    FROM elearning_v2.quiz_questions
                    WHERE quiz_id = %s
                """, (quiz_id,))

                correct_answers = dict(cur.fetchall())
                correct_count = 0

                for question in questions:
                    question_id = question[0]
                    answer = request.form.get(str(question_id), "")

                    if answer == correct_answers.get(question_id):
                        correct_count += 1

                score = round(correct_count * 100 / len(questions))

                cur.execute("""
                    INSERT INTO elearning_v2.quiz_attempts
                        (quiz_id, student_id, score, attempted_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                """, (quiz_id, session["user_id"], score))

                flash(
                    f"Результат: {correct_count} из {len(questions)}. "
                    f"Твой балл: {score}%.",
                    "success"
                )

                # Все тесты курса сданы — отмечаем курс завершённым
                cert = certificate_status(cur, session["user_id"], course[0])
                if cert and not cert["completed"] and cert["total"] and cert["passed"] >= cert["total"]:
                    try:
                        # отдельная точка сохранения: если база откажет, результат теста не пропадёт
                        with conn.transaction():
                            cur.execute("""
                                UPDATE elearning_v2.enrollments
                                SET status = 'completed'
                                WHERE student_id = %s AND course_id = %s
                                  AND status <> 'completed'
                            """, (session["user_id"], course[0]))
                        flash("Поздравляем! Все тесты сданы — курс завершён. "
                              "Теперь можно скачать сертификат и оставить отзыв.", "success")
                    except psycopg.Error:
                        app.logger.exception("Could not mark enrollment as completed")

                return redirect(url_for("history"))

    return render_template(
        "take_quiz.html",
        quiz=quiz,
        questions=questions
    )

@app.route("/history")
@login_required
def history():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    q.title,
                    qa.score,
                    qa.attempted_at
                FROM elearning_v2.quiz_attempts qa
                JOIN elearning_v2.quizzes q
                    ON q.quiz_id = qa.quiz_id
                WHERE qa.student_id = %s
                ORDER BY qa.attempted_at DESC
            """, (session["user_id"],))

            attempts = cur.fetchall()

    return render_template("history.html", attempts=attempts)

@app.route("/reviews")
def reviews():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.rating, r.comment, r.created_at,
                       u.full_name, c.title
                FROM elearning_v2.reviews r
                JOIN elearning_v2.enrollments e
                    ON e.enrollment_id = r.enrollment_id
                JOIN elearning_v2.users u
                    ON u.user_id = e.student_id
                JOIN elearning_v2.courses c
                    ON c.course_id = e.course_id
                ORDER BY r.created_at DESC
            """)
            review_list = cur.fetchall()

    return render_template("reviews.html", reviews=review_list)


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("profile"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not full_name or not email or not password:
            flash("Заполни все поля.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Пароль должен содержать минимум 8 символов.", "error")
            return render_template("register.html")

        if len(password) > 128:
            flash("Пароль слишком длинный.", "error")
            return render_template("register.html")

        password_hash = generate_password_hash(password)

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT user_id
                        FROM elearning_v2.users
                        WHERE LOWER(email) = %s
                    """, (email,))

                    if cur.fetchone():
                        flash("Этот email уже зарегистрирован.", "error")
                        return render_template("register.html")

                    cur.execute("""
                        INSERT INTO elearning_v2.users
                            (full_name, email, role, password_hash)
                        VALUES (%s, %s, %s, %s)
                        RETURNING user_id, full_name, email, role
                    """, (full_name, email, "student", password_hash))

                    user = cur.fetchone()

        except Exception:
            app.logger.exception("Registration failed")
            flash("Не удалось зарегистрироваться. Попробуй ещё раз.", "error")
            return render_template("register.html")

        session.clear()
        session["user_id"] = user[0]
        session["user_name"] = user[1]
        session["user_role"] = user[3]

        flash("Регистрация прошла успешно!", "success")
        return redirect(url_for("profile"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("profile"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, full_name, email, role, password_hash
                    FROM elearning_v2.users
                    WHERE LOWER(email) = %s
                """, (email,))

                user = cur.fetchone()

        if not user or not user[4] or not check_password_hash(user[4], password):
            flash("Неверный email или пароль.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user[0]
        session["user_name"] = user[1]
        session["user_role"] = user[3]

        flash("Ты успешно вошёл в аккаунт!", "success")
        return redirect(url_for("profile"))

    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("Ты вышел из аккаунта.", "success")
    return redirect(url_for("index"))


@app.route("/profile")
@login_required
def profile():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, full_name, email, role
                FROM elearning_v2.users
                WHERE user_id = %s
            """, (session["user_id"],))

            user = cur.fetchone()

    if user is None:
        session.clear()
        flash("Аккаунт не найден. Войди снова.", "error")
        return redirect(url_for("login"))

    user_data = {
        "user_id": user[0],
        "full_name": user[1],
        "email": user[2],
        "role": user[3]
    }

    return render_template("profile.html", user=user_data)


@app.context_processor
def inject_auth_user():
    return {
        "current_user": {
            "user_id": session.get("user_id"),
            "full_name": session.get("user_name"),
            "role": session.get("user_role")
        } if session.get("user_id") else None
    }


if __name__ == "__main__":
    app.run(debug=True)