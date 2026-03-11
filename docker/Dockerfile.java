FROM maven:3.9.6-eclipse-temurin-17 AS builder
WORKDIR /app
# Copy the proto definitions so the plugin can compile them
COPY proto/ /proto/
# Copy Java source
COPY backend-java/ /app/
# Build the application
RUN mvn clean package -DskipTests

FROM eclipse-temurin:17-jre-jammy
WORKDIR /app
COPY --from=builder /app/target/backend-java-0.0.1-SNAPSHOT.jar app.jar
EXPOSE 8080
CMD ["java", "-jar", "app.jar"]
