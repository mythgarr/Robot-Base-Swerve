from typing import Callable
import math
from wpilib import SmartDashboard, SendableChooser
from wpimath import units
from wpimath.controller import PIDController
from wpimath.filter import SlewRateLimiter
from wpimath.geometry import Rotation2d, Pose2d
from wpimath.kinematics import ChassisSpeeds, SwerveModulePosition, SwerveModuleState, SwerveDrive4Kinematics
from commands2 import Subsystem, Command
from lib import utils, logger
from lib.classes import ChassisLocation, MotorIdleMode, SpeedMode, DriveOrientation, OptionState, LockState
from lib.components.swerve_module import SwerveModule
import constants

class DriveSubsystem(Subsystem):
  def __init__(
      self, 
      getGyroHeading: Callable[[], units.degrees]
    ) -> None:
    super().__init__()
    self._getGyroHeading = getGyroHeading
    
    self._constants = constants.Subsystems.Drive

    self._swerveDriveModules = (
      SwerveModule(self._constants.kSwerveModules[0], self._constants.SwerveModule),
      SwerveModule(self._constants.kSwerveModules[1], self._constants.SwerveModule),
      SwerveModule(self._constants.kSwerveModules[2], self._constants.SwerveModule),
      SwerveModule(self._constants.kSwerveModules[3], self._constants.SwerveModule)
    )

    self._isDriftCorrectionActive: bool = False
    self._driftCorrectionThetaController = PIDController(
      self._constants.kDriftCorrectionThetaControllerPIDConstants.P, 
      self._constants.kDriftCorrectionThetaControllerPIDConstants.I, 
      self._constants.kDriftCorrectionThetaControllerPIDConstants.D
    )
    self._driftCorrectionThetaController.enableContinuousInput(-180.0, 180.0)
    self._driftCorrectionThetaController.setTolerance(
      self._constants.kDriftCorrectionThetaControllerPositionTolerance, 
      self._constants.kDriftCorrectionThetaControllerVelocityTolerance
    )

    self._isAlignedToTarget: bool = False
    self._targetAlignmentThetaController = PIDController(
      self._constants.kTargetAlignmentThetaControllerPIDConstants.P, 
      self._constants.kTargetAlignmentThetaControllerPIDConstants.I, 
      self._constants.kTargetAlignmentThetaControllerPIDConstants.D
    )
    self._targetAlignmentThetaController.enableContinuousInput(-180.0, 180.0)
    self._targetAlignmentThetaController.setTolerance(
      self._constants.kTargetAlignmentThetaControllerPositionTolerance, 
      self._constants.kTargetAlignmentThetaControllerVelocityTolerance
    )

    self._inputXFilter = SlewRateLimiter(self._constants.kInputRateLimitDemo)
    self._inputYFilter = SlewRateLimiter(self._constants.kInputRateLimitDemo)
    self._inputRotationFilter = SlewRateLimiter(self._constants.kInputRateLimitDemo)

    self._speedMode: SpeedMode = SpeedMode.Competition
    speedModeChooser = SendableChooser()
    speedModeChooser.setDefaultOption(SpeedMode.Competition.name, SpeedMode.Competition)
    speedModeChooser.addOption(SpeedMode.Demo.name, SpeedMode.Demo)
    speedModeChooser.onChange(lambda speedMode: setattr(self, "_speedMode", speedMode))
    SmartDashboard.putData("Robot/Drive/SpeedMode", speedModeChooser)

    self._orientation: DriveOrientation = DriveOrientation.Field
    orientationChooser = SendableChooser()
    orientationChooser.setDefaultOption(DriveOrientation.Field.name, DriveOrientation.Field)
    orientationChooser.addOption(DriveOrientation.Robot.name, DriveOrientation.Robot)
    orientationChooser.onChange(lambda orientation: setattr(self, "_orientation", orientation))
    SmartDashboard.putData("Robot/Drive/Orientation", orientationChooser)

    self._driftCorrection: OptionState = OptionState.Enabled
    driftCorrectionChooser = SendableChooser()
    driftCorrectionChooser.setDefaultOption(OptionState.Enabled.name, OptionState.Enabled)
    driftCorrectionChooser.addOption(OptionState.Disabled.name, OptionState.Disabled)
    driftCorrectionChooser.onChange(lambda driftCorrection: setattr(self, "_driftCorrection", driftCorrection))
    SmartDashboard.putData("Robot/Drive/DriftCorrection", driftCorrectionChooser)

    idleModeChooser = SendableChooser()
    idleModeChooser.setDefaultOption(MotorIdleMode.Brake.name, MotorIdleMode.Brake)
    idleModeChooser.addOption(MotorIdleMode.Coast.name, MotorIdleMode.Coast)
    idleModeChooser.onChange(lambda idleMode: self._setIdleMode(idleMode))
    SmartDashboard.putData("Robot/Drive/IdleMode", idleModeChooser)

    self._lockState: LockState = LockState.Unlocked

    SmartDashboard.putNumber("Robot/Drive/Chassis/Length", self._constants.kWheelBase)
    SmartDashboard.putNumber("Robot/Drive/Chassis/Width", self._constants.kTrackWidth)
    SmartDashboard.putNumber("Robot/Drive/Speed/Max", self._constants.kTranslationSpeedMax)

  def periodic(self) -> None:
    self._updateTelemetry()

  def driveCommand(
      self, 
      getInputX: Callable[[], units.percent], 
      getInputY: Callable[[], units.percent], 
      getInputRotation: Callable[[], units.percent]
    ) -> Command:
    return self.run(
      lambda: self._drive(getInputX(), getInputY(), getInputRotation())
    ).onlyIf(
      lambda: self._lockState != LockState.Locked
    ).withName("DriveSubsystem:Drive")

  def _drive(self, inputX: units.percent, inputY: units.percent, inputRotation: units.percent) -> None:
    if self._driftCorrection == OptionState.Enabled:
      isTranslating: bool = inputX != 0 or inputY != 0
      isRotating: bool = inputRotation != 0
      if isTranslating and not isRotating and not self._isDriftCorrectionActive:
        self._isDriftCorrectionActive = True
        self._driftCorrectionThetaController.reset()
        self._driftCorrectionThetaController.setSetpoint(self._getGyroHeading())
      elif isRotating or not isTranslating:
        self._isDriftCorrectionActive = False
      if self._isDriftCorrectionActive:
        inputRotation = self._driftCorrectionThetaController.calculate(self._getGyroHeading())
        if self._driftCorrectionThetaController.atSetpoint():
          inputRotation = 0

    if self._speedMode == SpeedMode.Demo:
      inputX = self._inputXFilter.calculate(inputX * self._constants.kInputLimitDemo)
      inputY = self._inputYFilter.calculate(inputY * self._constants.kInputLimitDemo)
      inputRotation = self._inputRotationFilter.calculate(inputRotation * self._constants.kInputLimitDemo)

    speedX: units.meters_per_second = inputX * self._constants.kTranslationSpeedMax
    speedY: units.meters_per_second = inputY * self._constants.kTranslationSpeedMax
    speedRotation: units.radians_per_second = inputRotation * self._constants.kRotationSpeedMax # type: ignore
    
    if self._orientation == DriveOrientation.Field:
      self.drive(ChassisSpeeds.fromFieldRelativeSpeeds(speedX, speedY, speedRotation, Rotation2d.fromDegrees(self._getGyroHeading())))
    else:
      self.drive(ChassisSpeeds(speedX, speedY, speedRotation))      

  def drive(self, chassisSpeeds: ChassisSpeeds) -> None:
    self._setSwerveModuleStates(
      self._constants.kSwerveDriveKinematics.toSwerveModuleStates(
        ChassisSpeeds.discretize(chassisSpeeds, 0.02)
      )
    )

  def _setSwerveModuleStates(self, swerveModuleStates: tuple[SwerveModuleState, ...]) -> None:
    SwerveDrive4Kinematics.desaturateWheelSpeeds(swerveModuleStates, self._constants.kTranslationSpeedMax)
    self._swerveDriveModules[0].setTargetState(swerveModuleStates[0])
    self._swerveDriveModules[1].setTargetState(swerveModuleStates[1])
    self._swerveDriveModules[2].setTargetState(swerveModuleStates[2])
    self._swerveDriveModules[3].setTargetState(swerveModuleStates[3])

  def getSpeeds(self) -> ChassisSpeeds:
    return self._constants.kSwerveDriveKinematics.toChassisSpeeds(self._getSwerveModuleStates())

  def getSwerveModulePositions(self) -> tuple[SwerveModulePosition, ...]:
    return (
      self._swerveDriveModules[0].getPosition(),
      self._swerveDriveModules[1].getPosition(),
      self._swerveDriveModules[2].getPosition(),
      self._swerveDriveModules[3].getPosition()
    )
  
  def _getSwerveModuleStates(self) -> tuple[SwerveModuleState, ...]:
    return (
      self._swerveDriveModules[0].getState(),
      self._swerveDriveModules[1].getState(),
      self._swerveDriveModules[2].getState(),
      self._swerveDriveModules[3].getState()
    )
  
  def _setIdleMode(self, idleMode: MotorIdleMode) -> None:
    self._swerveDriveModules[0].setIdleMode(idleMode)
    self._swerveDriveModules[1].setIdleMode(idleMode)
    self._swerveDriveModules[2].setIdleMode(idleMode)
    self._swerveDriveModules[3].setIdleMode(idleMode)
    SmartDashboard.putString("Robot/Drive/IdleMode/selected", idleMode.name)

  def lockCommand(self) -> Command:
    return self.startEnd(
      lambda: self._setLockState(LockState.Locked),
      lambda: self._setLockState(LockState.Unlocked)
    ).withName("DriveSubsystem:Lock")
  
  def _setLockState(self, lockState: LockState) -> None:
    self._lockState = lockState
    if lockState == LockState.Locked:
      self._swerveDriveModules[ChassisLocation.FrontLeft.value].setTargetState(SwerveModuleState(0, Rotation2d.fromDegrees(45)))
      self._swerveDriveModules[ChassisLocation.FrontRight.value].setTargetState(SwerveModuleState(0, Rotation2d.fromDegrees(-45)))
      self._swerveDriveModules[ChassisLocation.RearLeft.value].setTargetState(SwerveModuleState(0, Rotation2d.fromDegrees(-45)))
      self._swerveDriveModules[ChassisLocation.RearRight.value].setTargetState(SwerveModuleState(0, Rotation2d.fromDegrees(45)))

  def alignToTargetCommand(self, getRobotPose: Callable[[], Pose2d], getTargetHeading: Callable[[], units.degrees]) -> Command:
    return self.run(
      lambda: self._alignToTarget(getRobotPose().rotation().degrees())
    ).beforeStarting(
      lambda: [
        self.clearTargetAlignment(),
        self._targetAlignmentThetaController.reset(),
        self._targetAlignmentThetaController.setSetpoint(utils.wrapAngle(getTargetHeading() + self._constants.kTargetAlignmentHeadingInversion))  
      ]
    ).onlyIf(
      lambda: self._lockState != LockState.Locked
    ).until(
      lambda: self._isAlignedToTarget
    ).withName("DriveSubsystem:AlignToTarget")

  def _alignToTarget(self, robotHeading: units.degrees) -> None:
    speedRotation = self._targetAlignmentThetaController.calculate(robotHeading)
    speedRotation += math.copysign(self._constants.kTargetAlignmentCarpetFrictionCoeff, speedRotation)
    if self._targetAlignmentThetaController.atSetpoint():
      speedRotation = 0
      self._isAlignedToTarget = True
    self._setSwerveModuleStates(
      self._constants.kSwerveDriveKinematics.toSwerveModuleStates(
        ChassisSpeeds(0, 0, speedRotation)
      )
    )

  def isAlignedToTarget(self) -> bool:
    return self._isAlignedToTarget
  
  def clearTargetAlignment(self) -> None:
    self._isAlignedToTarget = False

  def reset(self) -> None:
    self._setIdleMode(MotorIdleMode.Brake)
    self.drive(ChassisSpeeds())
    self.clearTargetAlignment()
  
  def _updateTelemetry(self) -> None:
    SmartDashboard.putString("Robot/Drive/LockState", self._lockState.name)
    SmartDashboard.putBoolean("Robot/Drive/IsAlignedToTarget", self._isAlignedToTarget)
  